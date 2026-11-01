"""Use sgfmill for FF4 parsing, setup, captures and mainline replay."""
from sgfmill import sgf, boards
from .errors import ReviewError
from .models import GameRecord

COLS = 'ABCDEFGHJKLMNOPQRST'


def gtp(point, size):
    if point is None:
        return 'pass'
    row, col = point  # sgfmill uses bottom-left origin
    return f'{COLS[col]}{row + 1}'


def point_from_gtp(value, size):
    if value.lower() == 'pass':
        return None
    try:
        col, row = COLS.index(value[0]), int(value[1:]) - 1
        if not (0 <= col < size and 0 <= row < size):
            raise ValueError()
        return row, col
    except (ValueError, IndexError):
        raise ReviewError('invalid_coordinate', 'AI 返回了无效坐标。') from None


class SgfParser:
    def parse(self, content, *, source='sgf', source_game_id='', metadata=None):
        if len(content.encode('utf8')) > 1_000_000 or content.count(';') > 5000:
            raise ReviewError('sgf_size', '棋谱超过大小或节点限制。')
        # Bound tree recursion before entering the independent parser, ignoring SGF values.
        depth = 0
        inside = escaped = False
        for c in content:
            if escaped:
                escaped = False
            elif inside and c == '\\':
                escaped = True
            elif c == '[' and not inside:
                inside = True
            elif c == ']' and inside:
                inside = False
            elif not inside and c == '(':
                depth += 1
                if depth > 64:
                    raise ReviewError('sgf_depth', '棋谱变化分支过深。')
            elif not inside and c == ')':
                depth -= 1
        try:
            game = sgf.Sgf_game.from_string(content)
            root = game.get_root()
            size = game.get_size()
            if not 2 <= size <= 19:
                raise ValueError('size')
            if root.has_property('GM') and root.get('GM') != 1:
                raise ValueError('not Go')
            if metadata and metadata.get('fox_normalized_komi') is not None:
                root.set('KM', metadata['fox_normalized_komi'])
            game.root.set('CA', 'UTF-8')
            game.root.set('FF', 4)
            moves, snapshots, initial, initial_player = replay(game)
            if not moves or len(moves) > 1500:
                raise ValueError('moves')
            value = lambda key: root.get(key) if root.has_property(key) else None
            record = GameRecord(source, source_game_id, value('PB'), value('PW'), value('RE'),
                                len(moves), size, value('KM'), value('HA'),
                                game.serialise().decode('utf8'), metadata or {})
            record.metadata.update(date=value('DT'), rules=value('RU'),
                                   initial_stones=initial, initial_player=initial_player,
                                   moves=moves)
            return record
        except ReviewError:
            raise
        except (ValueError, KeyError, UnicodeError, IndexError, RecursionError, TypeError):
            raise ReviewError('invalid_sgf', '棋谱格式或主线落子不合法，无法安全分析。') from None


def replay(game):
    size = game.get_size()
    board = boards.Board(size)
    moves, snapshots, initial = [], [], []
    first_player = None
    last_colour = None
    ko = None
    for index, node in enumerate(game.get_main_sequence()):
        black, white, empty = node.get_setup_stones()
        if black or white or empty:
            if index > 0:
                raise ReviewError('midgame_setup', '主线含中途摆子编辑，V1 暂不分析此类棋谱。')
            if black & white or not board.apply_setup(black, white, empty):
                raise ValueError('setup')
            initial = [[c, gtp(p, size)] for c, points in [('B', black), ('W', white)] for p in sorted(points)]
        colour, point = node.get_move()
        if colour:
            if node.has_property('B') and node.has_property('W'):
                raise ValueError('two moves')
            if last_colour == colour:
                raise ValueError('turn order')
            first_player = first_player or colour.upper()
            snapshots.append(board.copy())
            if point is not None:
                if ko == point or board.get(*point) is not None:
                    raise ValueError('occupied or ko')
                ko = board.play(*point, colour)
                if board.get(*point) != colour:
                    raise ValueError('suicide')
            else:
                ko = None
            moves.append([colour.upper(), gtp(point, size)])
            last_colour = colour
    return moves, snapshots, initial, first_player
