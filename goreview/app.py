from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
from typing import Literal
import httpx
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .errors import ReviewError
from .models import GameRecord
from .report import Renderer, template_summary
from .service import ReviewService
from .sources import source_for
from .store import Store, STATES


class Submit(BaseModel):
    owner: str = Field(pattern=r'^[a-f0-9]{64}$')
    url: str = Field(max_length=2048)
    mode: Literal['coarse', 'quick', 'standard'] = 'coarse'


class Summary(BaseModel):
    owner: str = Field(pattern=r'^[a-f0-9]{64}$')
    text: str = Field(min_length=1, max_length=250)


def create_app(*, directory=None, token=None, service_factory=None):
    data = Path(directory or os.environ.get('GOREVIEW_DATA', './data'))
    secret = token if token is not None else os.environ.get('GOREVIEW_WORKER_TOKEN', '')
    if len(secret) < 32:
        raise ValueError('GOREVIEW_WORKER_TOKEN must contain at least 32 characters')
    store = Store(data / 'jobs.sqlite3')
    renderer = Renderer(data / 'reports', executable_path=os.environ.get('GOREVIEW_CHROMIUM') or None)

    @asynccontextmanager
    async def lifespan(app):
        proxy = os.environ.get('GOREVIEW_ANALYSIS_PROXY') or None
        if proxy and not proxy.startswith(('http://','https://')):
            raise ValueError('GOREVIEW_ANALYSIS_PROXY supports HTTP(S) proxies only')
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client, httpx.AsyncClient(
                timeout=120, trust_env=False, proxy=proxy) as analysis_client:
            if service_factory:
                service = service_factory(store, renderer, client)
            else:
                backend=os.environ.get('GOREVIEW_PROVIDER','zhizi')
                service = ReviewService(store, renderer, client, os.environ.get('ZHIZI_USER_TOKEN' if backend=='zhizi' else 'READ19_API_KEY',''),
                    backend=backend,allow_gpu=os.environ.get('GOREVIEW_ALLOW_GPU','false')=='true',
                    perspective_verified=os.environ.get('ZHIZI_PERSPECTIVE_VERIFIED','false')=='true',
                    komi_verified=os.environ.get('ZHIZI_KOMI_VERIFIED','false')=='true',
                    session_seconds=int(os.environ.get('GOREVIEW_SESSION_SECONDS','160')),
                    daily_limit=int(os.environ.get('GOREVIEW_DAILY_LIMIT',0)),
                    user_daily_limit=int(os.environ.get('GOREVIEW_USER_DAILY_LIMIT',0)),
                    concurrency=int(os.environ.get('GOREVIEW_CONCURRENCY',1)),
                    budget=int(os.environ.get('GOREVIEW_GAME_BUDGET',40000)),
                    scan_quick=int(os.environ.get('GOREVIEW_SCAN_VISITS',os.environ.get('GOREVIEW_QUICK_VISITS',8))),
                    cache_epoch=os.environ.get('GOREVIEW_CACHE_EPOCH','20261030'),
                    score_threshold=float(os.environ.get('GOREVIEW_SCORE_THRESHOLD',1.5)),
                    winrate_threshold=float(os.environ.get('GOREVIEW_WINRATE_THRESHOLD',.05)),
                    min_confident_visits=int(os.environ.get('GOREVIEW_MIN_CONFIDENT_VISITS',200)),
                    analysis_client=analysis_client)
            app.state.service = service
            await service.start()
            try:
                yield
            finally:
                await service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.renderer = store, renderer

    async def authorized(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization, 'Bearer '+secret):
            raise HTTPException(401, '未授权')

    @app.exception_handler(ReviewError)
    async def error(request, exc):
        return JSONResponse(status_code=400, content={'error': {'code':exc.code, 'message':exc.message}})

    def lookup(job_id, owner):
        if len(job_id) != 20 or any(c not in '0123456789abcdef' for c in job_id):
            raise ReviewError('not_found', '任务不存在。')
        return store.get(job_id, owner)

    @app.post('/v1/jobs', dependencies=[Depends(authorized)])
    async def submit(body: Submit):
        source = source_for(body.url)
        game_id = source.extract_id(body.url)
        # Canonicalize the supported URL; never download a user-supplied arbitrary URL.
        url = 'https://h5.foxwq.com/yehunewshare/?chessid='+game_id
        job = store.submit(body.owner, url, 'coarse')
        return {'id':job['id'], 'state':job['state'], 'label':STATES[job['state']]}

    @app.get('/v1/jobs/{job_id}', dependencies=[Depends(authorized)])
    async def status(job_id: str, owner: str):
        job = lookup(job_id, owner)
        result = {k:job[k] for k in ['id','state','error_code','error','cache_hit','created','updated']}
        result['label'] = STATES[job['state']]
        if job['state'] == 'done':
            payload = store.cached(job['cache_key'], ttl=365*86400)
            if not payload:
                raise ReviewError('cache_missing', '报告数据缺失。')
            record = GameRecord(**payload['record'])
            record.source_game_id = source_for(job['url']).extract_id(job['url'])
            result.update(summary=template_summary(record, payload['analysis']),
                          analysis=payload['analysis'], usage=payload['usage'])
        return result

    @app.get('/v1/jobs/{job_id}/image', dependencies=[Depends(authorized)])
    async def image(job_id: str, owner: str):
        job = lookup(job_id, owner)
        if job['state'] != 'done':
            raise ReviewError('not_ready', '报告尚未完成。')
        path = renderer.directory / (job_id+'.png')
        if not path.is_file():
            raise ReviewError('image_missing', '报告图片缺失。')
        return FileResponse(path, media_type='image/png')

    @app.post('/v1/jobs/{job_id}/summary', dependencies=[Depends(authorized)])
    async def summary(job_id: str, body: Summary):
        job = lookup(job_id, body.owner)
        if job['state'] != 'done':
            raise ReviewError('not_ready', '报告尚未完成。')
        payload = store.cached(job['cache_key'], ttl=365*86400)
        if not payload:
            raise ReviewError('cache_missing', '报告数据缺失。')
        record = GameRecord(**payload['record'])
        record.source_game_id = source_for(job['url']).extract_id(job['url'])
        await renderer.render(job_id, record,payload['analysis'],body.text)
        return {'ok': True}

    @app.get('/health', dependencies=[Depends(authorized)])
    async def health():
        service = getattr(app.state,'service',None)
        healthy = service is None or bool(service.tasks) and all(not task.done() for task in service.tasks)
        return JSONResponse(status_code=200 if healthy else 503,
                            content={'ok':healthy, 'provider':os.environ.get('GOREVIEW_PROVIDER','zhizi'),
                                     'gpu_enabled':os.environ.get('GOREVIEW_ALLOW_GPU','false')=='true',
                                     'analysis_key_configured':bool(os.environ.get('ZHIZI_USER_TOKEN','')) if os.environ.get('GOREVIEW_PROVIDER','zhizi')=='zhizi' else bool(os.environ.get('READ19_API_KEY',''))})

    return app
