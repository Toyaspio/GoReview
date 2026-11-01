import asyncio
from dataclasses import asdict
import logging
from .analysis import AnalysisSettings, Read19Provider, analyze_game
from .errors import ReviewError
from .models import GameRecord, parameter_hash
from .report import template_summary
from .sources import source_for

log = logging.getLogger(__name__)


class ReviewService:
    def __init__(self, store, renderer, client, key, *, daily_limit=0, user_daily_limit=0,
                 concurrency=1, budget=40000, scan_quick=8, scan_standard=128, deep_visits=512,
                 cache_epoch='20261030', timeout=120, job_timeout=1800,
                 score_threshold=1.5, winrate_threshold=.05, min_confident_visits=200,
                 analysis_client=None, backend='zhizi', allow_gpu=False,
                 perspective_verified=False, komi_verified=False, session_seconds=160):
        if not 1 <= concurrency <= 3 or daily_limit < 0 or user_daily_limit < 0:
            raise ValueError('invalid worker limits')
        if (not all(1 <= n <= 5000 for n in [scan_quick,scan_standard,deep_visits])
                or not 1 <= budget <= 1_000_000 or not 1 <= timeout <= 300
                or not .1 <= score_threshold <= 100 or not 0 <= winrate_threshold <= 1
                or not 1 <= min_confident_visits <= 5000):
            raise ValueError('invalid analysis limits')
        self.store, self.renderer, self.client, self.key = store, renderer, client, key
        self.analysis_client = analysis_client or client
        if backend not in ('zhizi','read19'):raise ValueError('unknown provider')
        self.backend,self.allow_gpu=backend,allow_gpu
        self.perspective_verified,self.komi_verified=perspective_verified,komi_verified
        if not 0 < session_seconds <= 160:raise ValueError('invalid session timeout')
        self.session_seconds=session_seconds
        self.daily_limit, self.user_daily_limit = daily_limit, user_daily_limit
        self.concurrency, self.budget = concurrency, budget
        self.scan_quick, self.scan_standard, self.deep_visits = scan_quick, scan_standard, deep_visits
        self.cache_epoch, self.timeout, self.job_timeout = cache_epoch, timeout, job_timeout
        self.thresholds = dict(score_threshold=score_threshold,winrate_threshold=winrate_threshold,
                               min_confident_visits=min_confident_visits)
        self.tasks = []
        self.locks = {}

    async def start(self):
        if self.tasks:
            return
        self.store.recover()
        self.tasks = [asyncio.create_task(self.loop()) for _ in range(self.concurrency)]

    async def close(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def loop(self):
        while True:
            job = self.store.claim()
            if not job:
                await asyncio.sleep(.5)
                continue
            await self.process(job)

    async def process(self, job):
        job_id = job['id']
        provider = None
        try:
            async with asyncio.timeout(self.job_timeout):
                record = await source_for(job['url']).fetch(job['url'], self.client)
                settings = AnalysisSettings(mode='coarse',
                    scan_visits=self.scan_quick,
                    deep_visits=self.deep_visits, game_budget=self.budget,
                    key_positions=0, cache_epoch='coarse-only-v1-'+self.cache_epoch,**self.thresholds)
                cache_key = record.content_hash + '-' + parameter_hash({**settings.parameters(),'provider':self.backend,'model':'28bnbt' if self.backend=='zhizi' else settings.preset})
                lock = self.locks.setdefault(cache_key, asyncio.Lock())
                async with lock:
                    cached = self.store.cached(cache_key)
                    if cached is None:
                        if self.backend=='zhizi' and not self.allow_gpu:
                            raise ReviewError('gpu_disabled','本阶段禁止真实GPU，未申请会话。')
                        if not self.key:
                            raise ReviewError('missing_key', '管理员尚未配置分析服务凭据。')
                        if (record.total_moves+1)*settings.scan_visits > settings.game_budget:
                            raise ReviewError('game_budget', '该棋局超出单局分析预算。')
                        self.store.reserve(job, self.daily_limit, self.user_daily_limit)
                        self.store.state(job_id, 'analyzing', cache_key=cache_key)
                        if self.backend=='read19':
                            provider = Read19Provider(self.key, self.analysis_client, timeout=self.timeout, preset=settings.preset)
                        else:
                            from .zhizi import ZhiziAuthClient,ZhiziSessionManager,ZhiziCloudProvider
                            provider = ZhiziCloudProvider(ZhiziSessionManager(ZhiziAuthClient(self.analysis_client,self.key),allow_gpu=self.allow_gpu,max_session_seconds=self.session_seconds),
                                perspective_verified=self.perspective_verified,komi_verified=self.komi_verified)
                        def intent(ceiling):
                            ledger = self.store.usage_for(job_id)
                            used = sum(max(r['usage']['visits'],r['ceiling']) if r['usage'] else r['ceiling'] for r in ledger)
                            if used+ceiling > self.budget:
                                raise ReviewError('game_budget', '已达到单局预算，停止后续分析。')
                            return self.store.intent(job_id, ceiling)
                        try:
                            analysis = await analyze_game(record, provider, settings, intent, self.store.usage)
                        finally:
                            if hasattr(provider,'close'):
                                await provider.close()
                                analysis_audit=provider.manager.audit
                                audit_id=self.store.intent(job_id,0)
                                self.store.usage(audit_id,{'visits':0,'session_audit':analysis_audit})
                        cached = {'record':asdict(record), 'analysis':analysis, 'parameters':settings.parameters(),
                                  'usage':self.store.usage_for(job_id)}
                        self.store.cache(cache_key, cached)
                    else:
                        analysis = cached['analysis']
                    self.store.state(job_id, 'rendering', cache_key=cache_key, cache_hit=bool(cached is not None and not self.store.usage_for(job_id)))
                    await self.renderer.render(job_id, record, analysis, template_summary(record, analysis))
                    self.store.state(job_id, 'done', cache_key=cache_key, cache_hit=not bool(self.store.usage_for(job_id)))
        except asyncio.CancelledError:
            self.store.state(job_id, 'failed', code='worker_interrupted', message='Worker 正在停止；不自动重试可能已计费的分析。')
            raise
        except ReviewError as exc:
            self.store.state(job_id, 'failed', code=exc.code, message=exc.message)
            log.warning('job=%s error=%s', job_id, exc.code)
        except Exception as exc:
            # Never log exception text: HTTP exception objects can contain auth headers or SGF data.
            self.store.state(job_id, 'failed', code='internal_error', message='复盘处理失败，请管理员检查 Worker 的错误类别记录。')
            log.error('job=%s exception_type=%s', job_id, type(exc).__name__)
        finally:
            # Keep memory bounded without breaking locks held by another queued task.
            for key, lock in list(self.locks.items()):
                if not lock.locked() and not getattr(lock, '_waiters', None):
                    self.locks.pop(key, None)
