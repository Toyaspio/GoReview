from dataclasses import dataclass, field
import hashlib
import json


@dataclass
class GameRecord:
    source: str
    source_game_id: str
    black_player: str | None
    white_player: str | None
    result: str | None
    total_moves: int
    board_size: int
    komi: float | None
    handicap: int | None
    sgf_content: str
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self):
        return hashlib.sha256(self.sgf_content.encode('utf8')).hexdigest()


def parameter_hash(parameters):
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()
