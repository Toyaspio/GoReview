"""Zhizi public-contract adapter. Real GPU access is disabled by default."""
import asyncio
import codecs
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlencode
from .analysis import AnalysisProvider, normalize_position
from .errors import ReviewError
from .http import bounded_json

ARGS = '--platform all --engine-type go --gpu-type 1x --kata-name katago-TENSORRT --kata-weight 28bnbt'


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ReviewError('zhizi_format', '智子费用数据不合法。') from None


class ZhiziAuthClient:
    def __init__(self, client, user_token):
        self.client, self._token = client, user_token

    @staticmethod
    def login_token(response):
        if not isinstance(response, dict) or not isinstance(response.get('token'), str) or not response['token']:
            raise ReviewError('zhizi_auth', '智子登录响应缺少凭据。')
        return response['token']

    async def request(self, method, path, **kwargs):
        allowed = {('GET','/api/cluster/balance'), ('GET','/api/cluster/usage/my-usages'),
                   ('POST','/api/cluster/account/fetch-socketio-token')}
        if (method,path) not in allowed or not self._token:
            raise ReviewError('zhizi_auth', '智子凭据缺失或操作未获允许。')
        try:
            async with asyncio.timeout(20):
                return await bounded_json(self.client,method,'https://www.zhizigo.com'+path,
                    headers={'Authorization':'Bearer '+self._token}, **kwargs)
        except ReviewError:
            raise
        except Exception:
            raise ReviewError('zhizi_network', '智子请求失败；未自动重试。') from None

    async def balance(self):
        value = await self.request('GET','/api/cluster/balance')
        return {'remaining':money(value['remainingBalance']), 'consumption':money(value['totalConsumption']),
                'connections':int(value['currentNumOfMyConnections'])}

    async def usages(self, start):
        value = await self.request('GET','/api/cluster/usage/my-usages',
                                   params={'from':start, 'page':0, 'pageSize':100})
        # Return only financial fields, never account IDs or connection usernames.
        threshold=datetime.fromisoformat(start.replace('Z','+00:00'))
        return [{'cost_yuan':str(money(row['totalCost'])), 'started_at':row['startedAt'],
                 'ended_at':row.get('endedAt'), 'finished':row['finished']}
                for row in value['items']
                if datetime.fromisoformat(row['startedAt'].replace('Z','+00:00')) >= threshold]


class KataGoOutputParser:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf8')('strict')
        self.buffer = ''

    def feed(self, chunk):
        if isinstance(chunk, dict):
            chunk = chunk.get('data')
        if isinstance(chunk, list):
            chunk = bytes(chunk)
        if isinstance(chunk,(bytes,bytearray,memoryview)):
            chunk = self.decoder.decode(bytes(chunk))
        if not isinstance(chunk,str):
            raise ReviewError('zhizi_stream', '不支持的引擎数据块。')
        self.buffer += chunk
        if len(self.buffer)>1_000_000:
            raise ReviewError('zhizi_stream', '引擎输出超过限制。')
        lines = self.buffer.split('\n')
        self.buffer = lines.pop()
        return [line.rstrip('\r') for line in lines]

    @staticmethod
    def candidates(line):
        found = []
        for part in re.split(r'\binfo\s+',line)[1:]:
            words = part.split(); row = {}; i = 0
            while i < len(words):
                key = words[i]; i += 1
                if key == 'pv':
                    row['pv'] = words[i:]; break
                if i == len(words):
                    raise ReviewError('zhizi_stream', '引擎候选数据不完整。')
                row[key] = words[i]; i += 1
            try:
                row['visits']=int(row['visits'])
                if row['visits']==0:continue  # unsearched candidate is not an evaluated move
                row['order']=int(row['order'])
                for key in ['winrate','scoreLead']:row[key]=float(row[key])
                if (not 0 <= row['winrate'] <= 1 or not math.isfinite(row['scoreLead'])
                        or row['visits']<1 or row['order']<0 or 'move' not in row):
                    raise ValueError()
            except (ValueError,KeyError):
                raise ReviewError('zhizi_candidate_values', '引擎候选字段缺失或数值不合法。') from None
            found.append(row)
        return found


class ZhiziSocketClient:
    def __init__(self, transport=None, timeout=15):
        if transport is None:
            import socketio
            transport = socketio.AsyncClient(reconnection=False,logger=False,engineio_logger=False)
        self.transport = transport
        self.timeout = timeout
        self.ready = asyncio.Event()
        self.disconnected = False
        self.lines = asyncio.Queue(maxsize=4096)
        self.parser = KataGoOutputParser()
        self.failure = None
        self.namespace = '/'
        self.connection_diagnostic = {}
        transport.on('ready',self._ready)
        transport.on('stdout',self._stdout)
        transport.on('stderr',self._stderr)
        transport.on('disconnect',self._disconnect)

    async def _ready(self,*args):self.ready.set()
    async def _disconnect(self,*args):
        self.disconnected=True
        self.failure=ReviewError('zhizi_disconnected','引擎连接中断；禁止复用旧状态或自动重启付费会话。')
        self.ready.set()
    async def _stderr(self,*args):
        # Never retain raw diagnostics: no tokens or externally supplied text in logs.
        pass
    async def _stdout(self,data):
        try:
            for line in self.parser.feed(data):self.lines.put_nowait(line)
        except Exception:
            self.connection_diagnostic['stream_payload_type']=type(data).__name__
            self.connection_diagnostic['stream_buffer_length']=len(self.parser.buffer)
            self.failure=ReviewError('zhizi_stream','引擎数据流损坏或超过限制。')

    async def connect(self, session):
        p=urlsplit(session['socketIOURL'])
        if (p.scheme!='https' or p.hostname not in ('web.zhizigo.com','www.zhizigo.com')
                or p.port not in (None,443) or p.username or p.password or p.query or p.fragment):
            raise ReviewError('zhizi_gateway','会话返回了尚未列入信任范围的网关。')
        url=session['socketIOURL']+'?'+urlencode({'zz-socketio-token':session['token']})
        self.namespace=p.path.rstrip('/') or '/'
        for name,handler in [('ready',self._ready),('stdout',self._stdout),('stderr',self._stderr),('disconnect',self._disconnect)]:
            self.transport.on(name,handler,namespace=self.namespace)
        try:
            await asyncio.wait_for(self.transport.connect(url,socketio_path='/socket.io.v4',transports=['websocket'],
                                         namespaces=[self.namespace],wait_timeout=self.timeout),self.timeout)
            self.connection_diagnostic={'phase':'waiting_ready'}
            await asyncio.wait_for(self.ready.wait(),self.timeout)
        except Exception as exc:
            self.connection_diagnostic={'phase':self.connection_diagnostic.get('phase','transport'),
                                        'error_type':type(exc).__name__,
                                        'frames':[{'file':Path(frame.filename).name,'function':frame.name,
                                                   'line':frame.lineno}
                                                  for frame in traceback.extract_tb(exc.__traceback__)]}
            if isinstance(exc,asyncio.IncompleteReadError):
                self.connection_diagnostic.update(expected=exc.expected,received=len(exc.partial))
            raise
        if self.failure:raise self.failure

    async def line(self):
        if self.failure:raise self.failure
        return await asyncio.wait_for(self.lines.get(),self.timeout)

    async def emit(self,command):
        await self.transport.emit('stdin',command,namespace=self.namespace)

    async def close(self):
        try:
            if getattr(self.transport,'connected',False):
                with suppress(Exception):
                    await asyncio.wait_for(self.emit('stop\n'),3)
        finally:
            with suppress(Exception):await asyncio.wait_for(self.transport.disconnect(),5)
            self.ready.clear()


class GTPController:
    def __init__(self, socket):
        self.socket=socket
        self.sequence=0
        self.position_key=None
        self.history=[]

    async def command(self, command, *, streaming=False):
        if '\n' in command or '\r' in command:
            raise ReviewError('gtp_command','指令含非法换行。')
        self.sequence+=1; number=self.sequence
        await self.socket.emit(f'{number} {command}\n')
        result=[]; matched=False
        async with asyncio.timeout(self.socket.timeout):
            while True:
                line=await self.socket.line()
                match=re.match(r'^([=?])(\d+)(?:\s|$)',line)
                if match:
                    if int(match[2])!=number:
                        raise ReviewError('gtp_order','引擎响应序号不一致。')
                    if match[1]=='?':raise ReviewError('gtp_rejected','引擎拒绝指令。')
                    matched=True
                    payload=line[match.end():].strip()
                    if payload:result.append(payload)
                    # kata-analyze is an OPEN response: its final blank line arrives only
                    # after stop/new command. Waiting for it here discards all analysis.
                    if streaming:return result
                elif matched and not line:
                    return result
                elif matched:result.append(line)

    async def sync(self, record, turn):
        rules={'chinese':'chinese','japanese':'japanese','aga':'aga','new zealand':'new-zealand','tromp-taylor':'tromp-taylor'}
        rule=rules.get((record.metadata.get('rules') or '').lower())
        if rule is None or record.komi is None:
            raise ReviewError('unknown_rules','规则或贴目未核验，停止分析。')
        stones=record.metadata['initial_stones']
        key=(record.board_size,record.komi,rule,tuple(tuple(s) for s in stones))
        history=[tuple(m) for m in record.metadata['moves'][:turn]]
        previous=list(self.history)
        incremental=self.position_key==key and history[:len(previous)]==previous
        self.position_key=None;self.history=[]
        if incremental:
            for colour,point in history[len(previous):]:
                await self.command(f'play {colour} {point}')
            self.position_key=key;self.history=history
            return
        for command in [f'boardsize {record.board_size}',f'kata-set-rules {rule}',f'komi {record.komi}','clear_board']:
            await self.command(command)
        if stones:
            if any(c!='B' for c,p in stones):
                raise ReviewError('unsupported_setup','当前协议未验证混合初始摆子，拒绝假造落子历史。')
            await self.command('set_free_handicap '+' '.join(p for c,p in stones))
        for colour,point in record.metadata['moves'][:turn]:
            await self.command(f'play {colour} {point}')
        self.position_key=key;self.history=history

    async def analyze(self, player, visits, *, interval_centiseconds=1, stop_after=True):
        if not 1 <= interval_centiseconds <= 100:raise ValueError('invalid analysis interval')
        await self.command(f'kata-analyze {player} {interval_centiseconds}',streaming=True)
        async with asyncio.timeout(self.socket.timeout):
            while True:
                line=await self.socket.line()
                if line.startswith('info '):
                    try:rows=KataGoOutputParser.candidates(line)
                    except ReviewError as exc:
                        tokens=set(line.split())
                        self.socket.connection_diagnostic['candidate_error']={
                            'code':exc.code,'fields_present':[k for k in ('move','visits','winrate','scoreLead','order','pv') if k in tokens]}
                        raise
                    best=next((r for r in rows if r['order']==0),None)
                    if best and best['visits']>=visits:
                        if stop_after:await self.command('stop')
                        return rows


class ZhiziSessionManager:
    def __init__(self, auth, *, allow_gpu=False, socket_factory=ZhiziSocketClient, max_session_seconds=60):
        self.auth=auth; self.allow_gpu=allow_gpu; self.socket_factory=socket_factory
        self.socket=None; self.before=None; self.started=None; self.audit={}
        self.watchdog=None; self.max_session_seconds=max_session_seconds
        self.open_attempted=False
        if not 0 < max_session_seconds <= 160:raise ValueError('session timeout must be <=160s')

    async def open(self):
        if not self.allow_gpu:
            raise ReviewError('gpu_disabled','本阶段禁止真实GPU；智子会话未申请。')
        if self.socket is not None:return self.socket
        if self.open_attempted:
            raise ReviewError('zhizi_session_closed','本次分析会话已结束，不自动重新申请计费会话。')
        self.open_attempted=True
        self.before=await self.auth.balance()
        # Disconnect acknowledgement can lag behind the preceding queued job.
        # Poll read-only before requesting a new billable session.
        for _ in range(15):
            if self.before['remaining']<=0:
                raise ReviewError('zhizi_balance','智子余额不足，请管理员检查账户余额。')
            if self.before['connections']==0:
                break
            await asyncio.sleep(2)
            self.before=await self.auth.balance()
        if self.before['remaining']<=0:
            raise ReviewError('zhizi_balance','智子余额不足，请管理员检查账户余额。')
        if self.before['connections']!=0:
            raise ReviewError('zhizi_busy','上一分析会话尚未释放，或账户正被其他程序使用，请稍后再试。')
        self.started=datetime.now(timezone.utc).isoformat()
        session=await self.auth.request('POST','/api/cluster/account/fetch-socketio-token',json={'args':ARGS})
        if not isinstance(session.get('token'),str) or not session['token']:
            raise ReviewError('zhizi_session','引擎临时凭据缺失。')
        self.socket=self.socket_factory()
        self.watchdog=asyncio.create_task(self._expire())
        try:await self.socket.connect(session)
        except BaseException as exc:
            await self.close()
            if isinstance(exc,asyncio.CancelledError):raise
            raise ReviewError('zhizi_connect','引擎未就绪或连接失败，已尝试释放会话。') from None
        return self.socket

    async def _expire(self):
        await asyncio.sleep(self.max_session_seconds)
        await self.close()

    async def close(self):
        watchdog,self.watchdog=self.watchdog,None
        if watchdog and watchdog is not asyncio.current_task():
            watchdog.cancel()
            with suppress(asyncio.CancelledError):await watchdog
        socket,self.socket=self.socket,None
        if socket:await socket.close()
        if self.started:
            self.audit={'started_at':self.started,'ended_at':datetime.now(timezone.utc).isoformat(),
                        'actual_cost_yuan':None,'cost_status':'pending_reconciliation'}
            if socket:self.audit['connection_diagnostic']=getattr(socket,'connection_diagnostic',{})
            try:
                after=await self.auth.balance()
                self.audit['account_consumption_delta_yuan']=str(after['consumption']-self.before['consumption'])
                self.audit['usage_records']=await self.auth.usages(self.started)
                # Aggregate account deltas are not falsely attributed to this particular session.
            except Exception:pass
            self.started=None


class ZhiziCloudProvider(AnalysisProvider):
    def __init__(self, manager, *, perspective_verified=False, komi_verified=False):
        self.manager=manager
        self.perspective_verified=perspective_verified; self.komi_verified=komi_verified

    async def analyze(self, record, turns, visits, on_intent, on_usage):
        if not self.manager.allow_gpu:
            raise ReviewError('gpu_disabled','本阶段禁止真实GPU；未申请会话。')
        if not self.perspective_verified or not self.komi_verified:
            raise ReviewError('verification_required','贴目或胜率视角尚未验收，禁止正式分析。')
        socket=await self.manager.open()
        controller=getattr(self,'_controller',None)
        if controller is None or controller.socket is not socket:
            controller=self._controller=GTPController(socket)
        balance=await self.manager.auth.balance()
        if balance['remaining']<=0:
            raise ReviewError('zhizi_balance','余额不足，停止分析。')
        positions={}; call_id=on_intent(len(turns)*visits)
        for turn in turns:
            await controller.sync(record,turn)
            moves=record.metadata['moves']
            player=moves[turn][0] if turn<len(moves) else ('W' if moves[-1][0]=='B' else 'B')
            rows=await controller.analyze(player,visits,stop_after=False)
            best=min(rows,key=lambda r:r['order'])
            positions[turn]=normalize_position({'turnNumber':turn,'rootInfo':{'currentPlayer':player,**best},'moveInfos':rows},
                                                record.board_size,perspective='SIDETOMOVE')
            positions[turn].update(source='zhizi',model='28bnbt',status='experimental',
                                   evaluation_kind='best_candidate_not_root',
                                   played=moves[turn][1] if turn<len(moves) else None)
        on_usage(call_id,{'visits':sum(p['visits'] for p in positions.values()),'provider':'zhizi',
                          'actual_cost_yuan':None,'cost_status':'pending_reconciliation'})
        return positions

    async def close(self):await self.manager.close()
