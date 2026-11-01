"""Read19 REST contract downloaded 2026-10-30; values explicitly requested as BLACK."""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
import math
import time
import uuid
import httpx
from .errors import ReviewError
from .http import bounded_json
from .sgf_parser import point_from_gtp


@dataclass(frozen=True)
class AnalysisSettings:
    mode: str = 'coarse'
    scan_visits: int = 8
    deep_visits: int = 256
    key_positions: int = 3
    game_budget: int = 40000
    preset: str = 'fast'
    cache_epoch: str = 'coarse-v1-20261031'
    score_threshold: float = 1.5
    winrate_threshold: float = .05
    min_confident_visits: int = 200

    def parameters(self):
        data=asdict(self)
        for obsolete in ('deep_visits','key_positions'):data.pop(obsolete,None)
        data['mode']='coarse'
        return data


class AnalysisProvider(ABC):
    @abstractmethod
    async def analyze(self, record, turns, visits, on_intent, on_usage): ...


class Read19Provider(AnalysisProvider):
    def __init__(self, key, client, *, timeout=120, preset='fast'):
        self.key, self.client, self.timeout, self.preset = key, client, timeout, preset

    async def analyze(self, record, turns, visits, on_intent, on_usage):
        if not self.key.startswith('kg_live_'):
            raise ReviewError('missing_key', '未配置 Read19 分析密钥。')
        if record.komi is None:
            raise ReviewError('unknown_komi', '棋谱未给出贴目，停止分析。')
        rules = (record.metadata.get('rules') or '').lower()
        mapped = {'chinese': 'chinese', 'japanese': 'japanese', 'aga': 'aga',
                  'new zealand': 'new-zealand', 'tromp-taylor': 'tromp-taylor'}
        if rules not in mapped:
            raise ReviewError('unknown_rules', '棋谱规则不明或尚不支持，停止分析。')
        query_id = uuid.uuid4().hex
        query = {'id': query_id, 'moves': record.metadata['moves'],
                 'initialStones': record.metadata['initial_stones'],
                 'initialPlayer': record.metadata['initial_player'],
                 'boardXSize': record.board_size, 'boardYSize': record.board_size,
                 'rules': mapped[rules], 'komi': record.komi, 'maxVisits': visits,
                 'analyzeTurns': turns,
                 'overrideSettings': {'reportAnalysisWinratesAs': 'BLACK'}}
        # Intent persists before submitting. Timeout / disconnect is an uncertain billable request.
        call_id = on_intent(len(turns) * visits)
        try:
            async with asyncio.timeout(self.timeout):
                result = await bounded_json(self.client, 'POST', 'https://read19.com/v1/analyze',
                    limit=16_000_000, headers={'Authorization': 'Bearer ' + self.key},
                    json={'preset': self.preset, 'query': query})
        except (httpx.TransportError, TimeoutError):
            raise ReviewError('analysis_uncertain', 'AI 请求超时或连接中断，可能已消耗额度；不自动重复提交。') from None
        usage = result.get('usage')
        if not isinstance(usage, dict) or not isinstance(usage.get('visits'), int) or usage['visits'] < 0:
            raise ReviewError('analysis_format', 'AI 用量返回不完整；已停止后续调用。')
        on_usage(call_id, usage)
        rows = result.get('results')
        if not isinstance(rows, list):
            raise ReviewError('analysis_format', 'AI 返回缺少分析数据。')
        parsed = {}
        for row in rows:
            if row.get('error') or row.get('warning'):
                raise ReviewError('engine_error', 'AI 引擎拒绝请求或返回警告，停止分析。')
            if row.get('isDuringSearch') is True:
                continue
            if row.get('id') != query_id:
                raise ReviewError('analysis_mismatch', 'AI 请求标识不一致。')
            position = normalize_position(row, record.board_size, perspective='BLACK')
            if position['turn'] not in turns or position['turn'] in parsed:
                raise ReviewError('analysis_mismatch', 'AI 返回了重复或错误局面。')
            turn = position['turn']
            moves = record.metadata['moves']
            expected = moves[turn][0] if turn < len(moves) else ('W' if moves[-1][0]=='B' else 'B')
            if position['player'] != expected:
                raise ReviewError('analysis_mismatch', 'AI 当前执子方与棋谱不一致。')
            parsed[position['turn']] = position
        if set(parsed) != set(turns):
            raise ReviewError('analysis_incomplete', 'AI 只返回部分局面；不会生成完整胜率报告。')
        return parsed


def normalize_position(row, size, *, perspective):
    try:
        root = row['rootInfo']
        player = root['currentPlayer']
        if player not in ('B', 'W'):
            raise ValueError()
        black = perspective == 'BLACK' or (perspective == 'SIDETOMOVE' and player == 'B')
        if perspective not in ('BLACK', 'WHITE', 'SIDETOMOVE'):
            raise ValueError()
        wr = float(root['winrate'])
        lead = root.get('scoreLead')
        lead = None if lead is None else float(lead)
        visits = int(root['visits'])
        turn = row['turnNumber']
        if (not isinstance(turn, int) or turn < 0 or not math.isfinite(wr) or not 0 <= wr <= 1
                or visits < 1 or (lead is not None and not math.isfinite(lead))):
            raise ValueError()
        best = min(row.get('moveInfos', []), key=lambda m: m['order'], default={})
        recommendation = best.get('move')
        pv = best.get('pv', [])[:16]
        if recommendation:
            point_from_gtp(recommendation, size)
        for move in pv:
            point_from_gtp(move, size)
        if not isinstance(pv, list):
            raise ValueError()
        candidates = None
        infos = row.get('moveInfos', [])
        if infos and any('visits' in item for item in infos):
            candidates = []
            seen = set()
            for item in infos:
                move, count, order = item['move'], int(item['visits']), int(item['order'])
                point_from_gtp(move, size)
                if count < 0 or order < 0 or move.upper() in seen:
                    raise ValueError()
                seen.add(move.upper())
                candidates.append({'move': move, 'visits': count, 'order': order})
            candidates.sort(key=lambda item: item['order'])
        return {'turn': turn, 'player': player, 'black_winrate': wr if black else 1-wr,
                'white_winrate': 1-wr if black else wr,
                'black_score_lead': lead if black or lead is None else -lead,
                'visits': visits, 'recommendation': recommendation, 'pv': pv, 'candidates': candidates,
                'confidence': '搜索预算证据；不代表统计置信概率'}
    except (ValueError, TypeError, KeyError):
        raise ReviewError('analysis_format', 'AI 分析字段、数值或坐标不合法。') from None


def move_agreement(record, positions):
    """Compare each actual move with the first choice before that move."""
    by_turn = {position['turn']: position for position in positions}
    result = {colour: {'matched': 0, 'compared': 0, 'total': 0, 'rate': None}
              for colour in ('B', 'W')}
    for turn, (colour, played) in enumerate(record.metadata['moves']):
        stats = result[colour]
        stats['total'] += 1
        recommended = by_turn.get(turn, {}).get('recommendation')
        if not recommended:
            continue
        stats['compared'] += 1
        if played.upper() == recommended.upper():
            stats['matched'] += 1
    for stats in result.values():
        if stats['compared']:
            stats['rate'] = stats['matched'] / stats['compared']
    return result


def mistakes(record, positions, settings):
    found = []
    for number, (colour, played) in enumerate(record.metadata['moves'], 1):
        before, after = positions[number-1], positions[number]
        sign = 1 if colour == 'B' else -1
        wr_loss = (before['black_winrate']-after['black_winrate'])*sign
        a, b = before['black_score_lead'], after['black_score_lead']
        loss = None if a is None or b is None else (a-b)*sign
        recommended = before['recommendation']
        # Different move alone is not a mistake; score evidence is required.
        if not recommended or played == recommended or loss is None or loss < settings.score_threshold:
            continue
        if wr_loss < settings.winrate_threshold and loss < settings.score_threshold * 2:
            continue
        low = min(before['visits'], after['visits']) < settings.min_confident_visits
        grade = ('关键胜负手' if wr_loss >= .2 and loss >= 5 and
                 (before['black_winrate']-.5)*(after['black_winrate']-.5) < 0
                 else '严重失误' if loss >= 5 and wr_loss >= .1
                 else '一般失误' if loss >= 3 else '疑问手')
        if low:
            grade = '候选失误（低预算，结论保留）'
        else:
            grade += '（粗扫候选）'
        found.append({'move_number': number, 'colour': colour, 'played': played,
                      'recommended': recommended, 'winrate_loss': wr_loss,
                      'score_loss': loss, 'grade': grade, 'low_confidence': low,
                      'candidate': True,
                      'pv': before['pv'], 'visits': min(before['visits'], after['visits']),
                      'explanation': '前后局面估计下降；低预算粗扫，仅作候选。' if low
                                     else '前后局面预期目差下降，结合胜率变化筛选。'})
    return sorted(found, key=lambda m: (m['score_loss'], m['winrate_loss']), reverse=True)


async def analyze_game(record, provider, settings, on_intent, on_usage):
    start = time.monotonic()
    turns = list(range(record.total_moves + 1))
    reserved = len(turns) * settings.scan_visits
    if reserved > settings.game_budget:
        raise ReviewError('game_budget', '该棋局超出单局分析预算，请降低搜索预算或提高管理员限额。')
    positions = {}
    # Bounded REST batches keep request size and timeout under control.
    for offset in range(0, len(turns), 16):
        positions.update(await provider.analyze(record, turns[offset:offset+16], settings.scan_visits,
                                               on_intent, on_usage))
    refine = []
    if set(positions) != set(turns):
        raise ReviewError('analysis_incomplete', '缺少局面，无法生成完整复盘。')
    return {'positions': [positions[n] for n in turns],
            'mistakes': mistakes(record, positions, settings), 'mode': 'coarse',
            'elapsed_seconds': round(time.monotonic()-start, 3), 'refined_turns': sorted(refine),
            'requested_visit_ceiling': reserved, 'perspective': 'BLACK'}
