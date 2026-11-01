from abc import ABC, abstractmethod
from urllib.parse import urlsplit, parse_qs
import re
from .errors import ReviewError
from .http import fetch_public_json
from .sgf_parser import SgfParser

TENCENT_MESSAGE = '目前支持野狐围棋棋谱，腾讯围棋解析功能暂未开放。'


class BaseGameSource(ABC):
    @abstractmethod
    def extract_id(self, url): ...

    @abstractmethod
    async def fetch(self, url, client): ...


def safe_parts(url):
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
        raise ReviewError('invalid_url', '请提供完整的公开棋谱链接。')
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port or p.fragment:
            raise ValueError()
        return p
    except ValueError:
        raise ReviewError('invalid_url', '棋谱链接格式不支持。') from None


class FoxwqSource(BaseGameSource):
    def extract_id(self, url):
        p = safe_parts(url)
        if p.hostname != 'h5.foxwq.com' or p.path not in ('/yehunewshare/', '/yehunewshare'):
            raise ReviewError('unsupported_source', '仅支持野狐公开棋谱分享链接。')
        ids = parse_qs(p.query, keep_blank_values=True).get('chessid', [])
        if len(ids) != 1 or not re.fullmatch(r'[0-9]{1,30}', ids[0]):
            raise ReviewError('invalid_id', '野狐链接缺少有效且唯一的 chessid。')
        return ids[0]

    async def fetch(self, url, client):
        game_id = self.extract_id(url)
        base = 'https://h5.foxwq.com/yehuDiamond/chessbook_local/'
        payload = await fetch_public_json(client, base + 'YHWQFetchChess?chessid=' + game_id)
        summary = await fetch_public_json(client, base + 'FetchChessSummaryByChessID?chessid=' + game_id)
        return self.from_payload(game_id, payload, summary)

    def from_payload(self, game_id, payload, summary):
        if not isinstance(payload, dict) or payload.get('result') != 0 or not isinstance(payload.get('chess'), str):
            raise ReviewError('not_found', '棋谱不存在、非公开或接口暂不可用。')
        info = summary.get('chesslist') if isinstance(summary, dict) else None
        if not isinstance(info, dict):
            raise ReviewError('not_found', '棋谱不存在、非公开或摘要不可用。')
        if str(info.get('chessid')) != game_id or str(payload.get('chessid')) != game_id:
            raise ReviewError('summary_missing', '棋谱摘要缺失或 ID 不一致，无法交叉验证。')
        if info.get('additionalrule') not in (None, 0):
            raise ReviewError('special_rule', '该棋谱使用特殊玩法，V1 暂不分析。')
        # Official live share JS explicitly maps rule 1 -> 7.5; raw Fox KM[375] is nonstandard.
        # Fox's share page restores default komi from the rule for ordinary even games.
        from sgfmill import sgf
        try:
            original = sgf.Sgf_game.from_string(payload['chess'])
            raw_komi = original.get_komi()
        except (ValueError, KeyError):
            raise ReviewError('invalid_sgf', '野狐返回的 SGF 不合法。') from None
        normalized_komi = None
        if raw_komi == 375 and info.get('rule') == 1 and info.get('handicap') == 0 and info.get('komi') == 375:
            normalized_komi = 7.5
        elif (raw_komi == 0 and info.get('rule') == 0 and info.get('handicap') == 0
              and info.get('komi') == 0 and original.get_root().get('RU') == 'Japanese'):
            # Official share getManualInfo: e.rule ? 7.5 : 6.5; HC=0 is even.
            normalized_komi = 6.5
        elif raw_komi is not None and abs(raw_komi) > 150:
            raise ReviewError('unknown_komi', '野狐贴目编码尚未验证，停止分析以免产生错误结论。')
        metadata = {'fox_raw_komi': raw_komi, 'fox_normalized_komi': normalized_komi,
                    'result_unit': '野狐原始结果（数值单位沿用来源）',
                    'summary_moves': info.get('movenum'), 'summary_winner': info.get('winner')}
        record = SgfParser().parse(payload['chess'], source='foxwq', source_game_id=game_id, metadata=metadata)
        names = [(record.black_player, info.get('blacknick'), info.get('blackenname')),
                 (record.white_player, info.get('whitenick'), info.get('whiteenname'))]
        winner = {'B': 1, 'W': 2}.get((record.result or '')[:1])
        if (any(not name or name not in (nick, en) for name, nick, en in names)
                or record.total_moves != info.get('movenum')
                or record.board_size != info.get('boardsize')
                or winner != info.get('winner')
                or record.handicap != info.get('handicap')):
            raise ReviewError('record_mismatch', '棋手、手数、结果或棋盘信息与摘要不一致。')
        if record.result and re.fullmatch(r'[BW]\+[0-9]+(?:\.[0-9]+)?', record.result):
            # Observed Fox non-score endings: RE[B+0] paired with point=-2/-3.
            # Winner was independently checked above; do not invent a margin or ending type.
            if float(record.result[2:]) == 0 and info.get('point') in (-2, -3):
                record.metadata['fox_raw_result'] = record.result
                record.metadata['fox_raw_point'] = info['point']
                record.result = record.result[0] + '+'
            elif abs(float(record.result[2:]) * 100 - info.get('point', -1)) > .001:
                raise ReviewError('result_mismatch', '棋谱结果数值与摘要不一致。')
        return record


class TencentWqSource(BaseGameSource):
    """Reserved adapter only. No network, JSONP or Playwright logic in V1."""
    def extract_id(self, url):
        p = safe_parts(url)
        if p.hostname != 'h5.txwq.qq.com' or p.path != '/txwqshare/index.html':
            raise ReviewError('unsupported_source', '不支持的棋谱来源。')
        ids = parse_qs(p.query).get('chessid', [])
        return ids[0] if len(ids) == 1 else None

    async def fetch(self, url, client):
        raise ReviewError('tencent_reserved', TENCENT_MESSAGE)


SOURCES = {'foxwq': FoxwqSource(), 'tencent': TencentWqSource()}


def source_for(url):
    p = safe_parts(url)
    if p.hostname == 'h5.txwq.qq.com':
        raise ReviewError('tencent_reserved', TENCENT_MESSAGE)
    source = SOURCES['foxwq']
    source.extract_id(url)
    return source
