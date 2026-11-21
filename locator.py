"""Finding where the game keeps its data on this particular machine / save.

The addresses in constants.py are where the data was found on the developer's machine. On other setups the same
structures sit at a different base but with the same offsets between them, so the client looks for the base itself:

* MEM2 save data (Bakugan table, cards, decks, rank/BP, stats, win counts): found through the two 36-byte player decks
  (ids padded with 0xFF, fixed shape), then confirmed by checking the other structures at their expected offsets.
  One number comes out of it: `delta`, to be added to every constants.py MEM2 save-data address.
* MEM1 enemy decks (16 x 6 decks): found by their strict layout. Second `delta`, used only for the enemy deck block.

Pure functions that take `read(address, size) -> bytes`, so they are testable without Dolphin.
"""
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import enemy_decks
from bakugan_data import VALID_COMBINED
from constants import (BP_MAX, BAKUGAN_RECORD_STRIDE, BP_ADDR, CARD_BASE, CARD_COUNT, DECK1_ADDR, DECK2_ADDR,
                       RANK_POINTS_ADDR, ROOT_BAKUGAN_ANCHOR, STATS_BLOCK_ADDR, STATS_BLOCK_LEN)

ReadFn = Callable[[int, int], bytes]

MEM2_START, MEM2_SIZE = 0x90000000, 0x04000000
MEM1_START, MEM1_SIZE = 0x80000000, 0x01800000
CHUNK = 0x400000
# logical (constants.py) address windows each delta applies to
MEM2_WINDOW = (0x90400000, 0x90500000)
ENEMY_WINDOW = (0x8033A000, 0x8033F000)

_BAKU = rb"(?:[\x00\x01][\x00-\xff]|\xff\xff)"
_GATE = rb"(?:\x00[\x00-\x61]|\xff\xff)"
_ABIL = rb"(?:\x00[\x62-\xbe]|\xff\xff)"
DECK_RE = re.compile(rb"(?=(" + _BAKU * 3 + rb"\xff{6}" + _GATE * 3 + rb"\xff{6}" + _ABIL * 3 + rb"\xff{6}))", re.S)
ENEMY_FIRST_RE = re.compile(
    rb"(?=([\x00-\x41]\x00{3}[\x00-\x05][\x00-\xff]{2}\x00[\x01-\x64]{5}[\x01-\x63]\x00{6}))", re.S)


@dataclass
class Candidate:
    delta: int
    score: int
    notes: List[str] = field(default_factory=list)
    bp: Optional[int] = None
    rank: Optional[int] = None


def _chunks(read: ReadFn, start: int, size: int):
    """Yield (address, bytes) for the readable parts of a region; unreadable chunks are skipped."""
    for off in range(0, size, CHUNK):
        n = min(CHUNK + 0x100, size - off)      # small overlap so a pattern across a chunk edge isn't missed
        try:
            yield start + off, read(start + off, n)
        except Exception:
            continue


def _deck_ok(raw: bytes) -> Optional[List[int]]:
    """A 36-byte player deck: returns the Bakugan combined ids in it, or None if the shape is wrong."""
    if len(raw) != 36:
        return None
    ids = []
    for i in range(3):
        v = int.from_bytes(raw[i * 2:i * 2 + 2], "big")
        if v != 0xFFFF:
            if v not in VALID_COMBINED:
                return None
            ids.append(v)
    if raw[6:12] != b"\xff" * 6 or raw[18:24] != b"\xff" * 6 or raw[30:36] != b"\xff" * 6:
        return None
    for i in range(3):
        g = int.from_bytes(raw[12 + i * 2:14 + i * 2], "big")
        a = int.from_bytes(raw[24 + i * 2:26 + i * 2], "big")
        if g != 0xFFFF and g > 97:
            return None
        if a != 0xFFFF and not 98 <= a <= 190:
            return None
    return ids


def check_mem2(read: ReadFn, delta: int) -> Optional[Candidate]:
    """Score the hypothesis 'save data is shifted by delta'. None when something required doesn't fit."""
    try:
        deck1 = read(DECK1_ADDR + delta, 36)
        deck2 = read(DECK2_ADDR + delta, 36)
        cards = read(CARD_BASE + delta, CARD_COUNT)
        econ = read(RANK_POINTS_ADDR + delta, 8)
        stats = read(STATS_BLOCK_ADDR + delta, STATS_BLOCK_LEN)
    except Exception:
        return None
    ids1 = _deck_ok(deck1)
    if ids1 is None or not ids1:
        return None                                   # deck 1 must be a real deck
    ids2 = _deck_ok(deck2)
    if ids2 is None and any(b not in (0x00, 0xFF) for b in deck2):
        return None
    if any(b > 1 for b in cards):
        return None                                   # card ownership bytes are 0 / 1
    cand = Candidate(delta=delta, score=1)
    rank, bp = int.from_bytes(econ[:4], "big"), int.from_bytes(econ[4:], "big")
    cand.rank, cand.bp = rank, bp
    if bp <= BP_MAX:
        cand.score += 1
    if rank <= 10_000_000:
        cand.score += 1
    if all(stats[20 + i] <= 99 for i in range(16)):
        cand.score += 1
    if all(int.from_bytes(stats[i:i + 2], "big") <= 9999 for i in (0, 2, 4, 12, 14, 16)):
        cand.score += 1
    # decks only hold what the player owns: cards owned, Bakugan present in the table
    for i in range(3):
        for off, lo, hi in ((12, 0, 97), (24, 98, 190)):
            v = int.from_bytes(deck1[off + i * 2:off + i * 2 + 2], "big")
            if v != 0xFFFF and lo <= v <= hi and cards[v] == 1:
                cand.score += 1
    for combined in ids1:
        try:
            rec = read(ROOT_BAKUGAN_ANCHOR + delta + combined * BAKUGAN_RECORD_STRIDE, 14)
        except Exception:
            continue
        if rec[13] >= 1 and rec[0] * 6 + rec[4] == combined:
            cand.score += 3                           # the Bakugan in the deck really is in the Bakugan table
            cand.notes.append(f"Bakugan {combined} found in table")
    return cand


def find_mem2(read: ReadFn, hints=()) -> List[Candidate]:
    """Candidates best-first. Tries the hints (last known delta, 0) before scanning all of MEM2."""
    seen = {}
    for h in hints:
        c = check_mem2(read, h)
        if c:
            seen[h] = c
    if seen:
        return sorted(seen.values(), key=lambda c: (-c.score, abs(c.delta)))
    for base, buf in _chunks(read, MEM2_START, MEM2_SIZE):
        for m in DECK_RE.finditer(buf):
            p = base + m.start()
            d = p - DECK1_ADDR
            if d in seen:
                continue
            c = check_mem2(read, d)
            if c:
                seen[d] = c
    return sorted(seen.values(), key=lambda c: (-c.score, abs(c.delta)))


def best(cands: List[Candidate]) -> Optional[Candidate]:
    """The winner, or None when the top candidates tie (the user must choose)."""
    if not cands:
        return None
    if len(cands) == 1 or cands[0].score > cands[1].score:
        return cands[0]
    return None


def check_enemy(read: ReadFn, delta: int) -> bool:
    try:
        block = read(enemy_decks.ENEMY_DECK_BASE + delta, enemy_decks.BLOCK_LEN)
    except Exception:
        return False
    return enemy_decks.structure_problem(block) is None


def find_enemy(read: ReadFn, hints=()) -> List[int]:
    """Deltas where all 96 enemy decks parse, nearest to the default first."""
    found = [h for h in hints if check_enemy(read, h)]
    if found:
        return sorted(set(found), key=abs)
    out = set()
    for base, buf in _chunks(read, MEM1_START, MEM1_SIZE):
        for m in ENEMY_FIRST_RE.finditer(buf):
            p = base + m.start()
            d = p - enemy_decks.ENEMY_DECK_BASE
            if d not in out and check_enemy(read, d):
                out.add(d)
    return sorted(out, key=abs)
