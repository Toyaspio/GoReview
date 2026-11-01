"""Metrics from the candidate snapshot before each move; no engine requests."""
from .analysis import move_agreement


def move_metrics(record, positions, *, window=20):
    if type(window) is not int or window < 1:
        raise ValueError('positive window required')
    by_turn = {p['turn']: p for p in positions}
    first = move_agreement(record, positions)
    weighted = {c: {'rate': None, 'compared': 0, 'total': first[c]['total']} for c in ('B', 'W')}
    history = {c: [] for c in ('B', 'W')}
    trend = {c: {'weighted': [], 'first_choice': []} for c in ('B', 'W')}

    def average(values):
        available = [v for v in values if v is not None]
        return sum(available) / len(available) if available else None

    for turn, (colour, played) in enumerate(record.metadata['moves']):
        before = by_turn.get(turn, {})
        choice = before.get('recommendation')
        hit = float(played.upper() == choice.upper()) if choice else None
        candidates = before.get('candidates')
        degree = None
        if candidates:
            highest = max(item['visits'] for item in candidates)
            if highest > 0:
                actual = next((item['visits'] for item in candidates
                               if item['move'].upper() == played.upper()), 0)
                degree = actual / highest
        history[colour].append((degree, hit))
        for c in ('B', 'W'):
            recent = history[c][-window:]
            for index, key in enumerate(('weighted', 'first_choice')):
                value = average([row[index] for row in recent]) if c == colour else None
                trend[c][key].append(None if value is None else round(value * 100, 3))
    for c in ('B', 'W'):
        values = [row[0] for row in history[c] if row[0] is not None]
        weighted[c].update(rate=average(values), compared=len(values))
    return {'first_choice': first, 'weighted': weighted, 'trend': trend,
            'turns': list(range(1, record.total_moves + 1)), 'window': window,
            'definition': 'visits_ratio_to_max_v1'}
