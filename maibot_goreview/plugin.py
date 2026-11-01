import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlsplit
import httpx
from maibot_sdk import Command, HookHandler, Field, MaiBotPlugin, PluginConfigBase
from .delivery import DeliveryStore
from .trigger import mentioned_fox_request


class PluginSection(PluginConfigBase):
    config_version: str = Field(default='0.1.0', description='配置版本')
    enabled: bool = Field(default=False, description='启用围棋复盘，可能消耗云端额度')
    allowed_groups: list[str] = Field(default_factory=list,description='允许复盘的真实群标识；留空关闭自动入口')
    receipt: str = Field(default='阿羽收到棋谱啦～正在认真看你的棋哦，稍等我一会儿呀 ♟️',description='任务接收提示')


class WorkerSection(PluginConfigBase):
    url: str = Field(default='http://goreview-worker:8097', description='管理员配置的独立 Worker 内网地址')
    token: str = Field(default='', repr=False, json_schema_extra={'input_type':'password'},
                       description='优先使用 GOREVIEW_WORKER_TOKEN；至少32字符')
    llm_summary: bool = Field(default=True, description='使用现有 Replyer 生成解释，失败使用基础摘要')


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    worker: WorkerSection = Field(default_factory=WorkerSection)


def accepted_summary(text, fallback):
    # Constrained composition: Replyer selects a verified wording, never rewrites numerical facts.
    try:
        data = json.loads(text)
        index = data['variant']
        if type(index) is not int or not 0 <= index <= 2:
            return fallback
        selected = summary_variants(fallback)[index]
        return selected if 100 <= len(selected) <= 250 else fallback
    except (ValueError,KeyError,TypeError):
        return fallback


def summary_variants(fallback):
    return [fallback, '阿羽把这盘值得回看的地方整理好了。'+fallback,
            '咱们先把关键选择摆出来看。'+fallback]


class GoReviewPlugin(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self.client = None
        self.poller = None
        self.deliveries = None
        self.token = ''
        self.lifecycle = asyncio.Lock()

    async def on_load(self):
        async with self.lifecycle:
            if self.client is not None:
                return
            self.token = os.environ.get('GOREVIEW_WORKER_TOKEN') or self.config.worker.token
            if len(self.token) < 32:
                self.ctx.logger.warning('GoReview not ready: worker_token_missing')
                return
            url = urlsplit(self.config.worker.url)
            if (url.scheme not in ('http','https') or url.username or url.password or url.query or url.fragment
                    or url.hostname not in ('goreview-worker','127.0.0.1','localhost') or url.path not in ('','/')):
                self.ctx.logger.error('GoReview not ready: invalid_worker_address')
                return
            self.ctx.paths.data_dir.mkdir(parents=True,exist_ok=True)
            self.deliveries = DeliveryStore(self.ctx.paths.data_dir/'delivery.sqlite3')
            self.client = httpx.AsyncClient(base_url=self.config.worker.url,
                headers={'Authorization':'Bearer '+self.token}, timeout=15, trust_env=False, follow_redirects=False)
            self.poller = asyncio.create_task(self._poll())

    async def on_unload(self):
        async with self.lifecycle:
            task, self.poller = self.poller, None
            if task:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            if self.client:
                await self.client.aclose()
                self.client = None

    async def on_config_update(self, scope, config_data, version):
        if scope == 'self':
            await self.on_unload()
            await self.on_load()

    def _owner(self, kwargs):
        user = str(kwargs.get('user_id') or '')
        message = kwargs.get('message') or {}
        account = str(message.get('account_id') or '')
        scope = str(message.get('scope') or '')
        platform = str(kwargs.get('platform') or '')
        # RouteKey.scope is optional; an empty scope is a real account-level route.
        if not all((user,account,platform)):
            raise ValueError('请求缺少可靠的账号、作用域或用户身份。')
        # OpenID is kept in its real account/scope; never treated as QQ or merged by nickname.
        identity = json.dumps([platform,account,scope,str(kwargs.get('group_id') or ''),user],ensure_ascii=True,separators=(',',':'))
        return hmac.new(self.token.encode(),identity.encode(),hashlib.sha256).hexdigest()

    async def _json(self, method, path, **kwargs):
        async with self.client.stream(method,path,**kwargs) as response:
            chunks = bytearray()
            async for part in response.aiter_bytes():
                chunks.extend(part)
                if len(chunks) > 2_000_000:
                    raise ValueError('Worker 响应超过限制。')
            try:
                payload = json.loads(chunks)
            except ValueError:
                raise ValueError('Worker 响应格式异常。') from None
            if response.status_code != 200:
                error = payload.get('error',{})
                raise ValueError(error.get('message','Worker 请求失败。') if isinstance(error,dict) else 'Worker 请求失败。')
            return payload

    async def _say(self, text, stream):
        try:
            result = await self.ctx.send.text(text,stream)
            return result.get('sent',False) if isinstance(result,dict) else bool(result)
        except Exception:
            return False

    @HookHandler('chat.receive.after_process',name='go_review_mention',timeout_ms=15000)
    async def receive_hook(self,message=None,**kwargs):
        result=await self.on_mention(message=message,**kwargs)
        return {'action':'continue' if result['continue_processing'] else 'abort'}

    async def on_mention(self,message=None,**kwargs):
        if not self.config.plugin.enabled or self.client is None:return {'continue_processing':True}
        try:
            request=mentioned_fox_request(message)
            if not request or request['group_id'] not in self.config.plugin.allowed_groups:
                return {'continue_processing':True}
            if not request['stream_id'] or not message.get('message_id'):
                return {'continue_processing':True}
            owner=self._owner(request)
            key=hmac.new(self.token.encode(),json.dumps([owner,request['stream_id'],message['message_id']]).encode(),hashlib.sha256).hexdigest()
            if not self.deliveries.claim_message(key):return {'continue_processing':False}
            await self.submit(**request,matched_groups={'url':request['url']})
            return {'continue_processing':False}
        except ValueError as exc:
            info=(message or {}).get('message_info') or {}
            group=str((info.get('group_info') or {}).get('group_id') or '')
            if group in self.config.plugin.allowed_groups and (message or {}).get('session_id'):
                await self._say(str(exc),message['session_id'])
                return {'continue_processing':False}
            return {'continue_processing':True}

    @Command('go_review', pattern=r'\A/复盘[ \t]+(?P<url>https?://[^\s]+)(?:[ \t]+(?P<mode>粗扫|快速|标准))?[ \t]*\Z')
    async def submit(self, **kwargs):
        stream = kwargs.get('stream_id')
        try:
            if not self.config.plugin.enabled or self.client is None:
                raise ValueError('围棋复盘尚未启用或 Worker 配置未完成。')
            if not stream or not kwargs.get('group_id'):
                raise ValueError('请在群聊中提交复盘。')
            if self.config.plugin.allowed_groups and str(kwargs['group_id']) not in self.config.plugin.allowed_groups:
                raise ValueError('这个群还没有开放围棋复盘哦～')
            groups = kwargs.get('matched_groups') or {}
            # Only the worker performs trusted-source fetching and authoritative validation.
            owner = self._owner(kwargs)
            result = await self._json('POST','/v1/jobs',json={'owner':owner,'url':groups.get('url',''),
                                     'mode':'coarse'})
            self.deliveries.add(result['id'],owner,stream)
            await self._say(self.config.plugin.receipt,stream)
            return True, '已提交后台任务', True
        except (ValueError,httpx.HTTPError) as exc:
            text = str(exc) if isinstance(exc,ValueError) else 'Worker 暂时无法连接，请稍后重试。'
            if stream:
                await self._say(text,stream)
            return False, text, True

    @Command('go_review_progress', pattern=r'\A/复盘进度[ \t]+(?P<job>[a-f0-9]{20})[ \t]*\Z')
    async def progress(self, **kwargs):
        stream = kwargs.get('stream_id')
        try:
            if not self.config.plugin.enabled or not self.client:
                raise ValueError('围棋复盘尚未启用。')
            job = (kwargs.get('matched_groups') or {}).get('job','')
            result = await self._json('GET','/v1/jobs/'+job,params={'owner':self._owner(kwargs)})
            delivery = self.deliveries.get(job)
            extra = f'；投递状态：{delivery["state"]}' if delivery else ''
            text = result['label'] + (('：'+result['error']) if result.get('error') else '')+extra
        except (ValueError,httpx.HTTPError) as exc:
            text = str(exc) if isinstance(exc,ValueError) else 'Worker 暂时无法连接。'
        await self._say(text,stream)
        return True,text,True

    @Command('go_review_help', pattern=r'\A/复盘帮助[ \t]*\Z')
    async def help(self, **kwargs):
        text = ('发送 /复盘 野狐分享链接，查看全盘走势和三个关键失误。\n'
                '/复盘进度 任务ID 查询自己的任务。\n'
                '目前支持野狐公开棋谱，腾讯围棋解析暂未开放。')
        await self._say(text,kwargs.get('stream_id'))
        return True,text,True

    async def _poll(self):
        while True:
            try:
                if self.config.plugin.enabled:
                    for delivery in self.deliveries.waiting():
                        await self._check_delivery(delivery)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning('GoReview polling failure: %s',type(exc).__name__)
            await asyncio.sleep(3)

    async def _check_delivery(self, item):
        job, owner, stream = item['job'],item['owner'],item['stream']
        result = await self._json('GET','/v1/jobs/'+job,params={'owner':owner})
        if result['state']=='failed':
            self.deliveries.state(job,'sending')
            sent = await self._say('复盘失败：'+result.get('error','未知错误'),stream)
            self.deliveries.state(job,'failed' if sent else 'uncertain')
            return
        if result['state']!='done':
            return
        self.deliveries.state(job,'preparing')
        try:
            summary = result['summary']
            fallback = summary
            if self.config.worker.llm_summary:
                try:
                    reference = json.dumps({'verified_variants':summary_variants(summary)},ensure_ascii=False)
                    response = await asyncio.wait_for(self.ctx.maisaka.reply.generate(stream,reference,
                        format_instruction='按阿羽当前性格，从提供的三份已核验解说中选最自然的一份。只返回JSON：{"variant":0}，variant为0、1或2。不改写数据、不添加棋局判断。',
                        timeout_ms=65000),timeout=60)
                    if isinstance(response,dict) and response.get('success'):
                        summary = accepted_summary(response.get('text'),summary)
                except Exception:
                    pass
            if summary != fallback:
                try:
                    await self._json('POST',f'/v1/jobs/{job}/summary',
                                     json={'owner':owner,'text':summary},timeout=90)
                except Exception:
                    summary = fallback
            image_data = bytearray()
            async with self.client.stream('GET',f'/v1/jobs/{job}/image',params={'owner':owner}) as response:
                if response.status_code!=200:
                    raise ValueError('image_unavailable')
                async for chunk in response.aiter_bytes():
                    image_data.extend(chunk)
                    if len(image_data)>12_000_000:
                        raise ValueError('image_too_large')
            if not image_data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('not_png')
            self.deliveries.state(job,'sending',summary)
            sent = await self.ctx.send.image(base64.b64encode(image_data).decode(),stream)
            sent = sent.get('sent',False) if isinstance(sent,dict) else bool(sent)
            if not sent:
                self.deliveries.state(job,'uncertain')
                return
            text_sent = await self._say(summary,stream)
            self.deliveries.state(job,'sent' if text_sent else 'image_sent_text_uncertain')
        except asyncio.CancelledError:
            self.deliveries.state(job,'uncertain')
            raise
        except Exception as exc:
            self.deliveries.state(job,'uncertain')
            self.ctx.logger.warning('GoReview delivery job=%s exception=%s',job,type(exc).__name__)


def create_plugin():
    return GoReviewPlugin()
