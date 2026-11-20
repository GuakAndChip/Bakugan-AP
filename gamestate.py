"""Reads the player's progress counters out of Dolphin memory in one batch."""
from dataclasses import dataclass
from typing import List, Optional

from constants import (BP_ADDR, ECON_BLOCK_LEN, OPPONENTS, RANK_POINTS_ADDR, STATS_BLOCK_ADDR,
                       STATS_BLOCK_LEN)
from memory import MemoryAccessError


@dataclass
class GameState:
    wins: List[int]                  # per-opponent win counters (Dan..Marduk)
    battles: int
    total_wins: int
    losses: int
    least_turns: int                 # fewest turns in a won battle (0 = none recorded yet)
    one_on_one: int
    tag_team: int
    battle_royale: int
    rank_points: Optional[int]
    money: Optional[int]


def read_game_state(mem) -> GameState:
    """Raises MemoryAccessError if the game's memory can't be read."""
    block = mem.read(STATS_BLOCK_ADDR, STATS_BLOCK_LEN)

    def be16(offset: int) -> int:
        return int.from_bytes(block[offset:offset + 2], "big")

    rank = money = None
    try:
        econ = mem.read(RANK_POINTS_ADDR, ECON_BLOCK_LEN)   # rank points then BP, 4 bytes each
        rank = int.from_bytes(econ[:4], "big")
        money = int.from_bytes(econ[4:8], "big")
    except MemoryAccessError:
        pass
    return GameState(
        wins=list(block[20:20 + len(OPPONENTS)]),
        battles=be16(0), total_wins=be16(2), losses=be16(4), least_turns=block[6],
        one_on_one=be16(12), tag_team=be16(14), battle_royale=be16(16),
        rank_points=rank, money=money,
    )
