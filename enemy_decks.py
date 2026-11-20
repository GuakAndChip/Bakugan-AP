"""Enemy deck randomization, difficulty scaling and nerfs.

Pure functions (no memory access): the engine reads the original decks once (the "baseline"), this module turns the
baseline into the decks that should be in memory, and the engine writes them. Everything is derived from the seed in
slot_data, so every player on the same seed gets the same decks and nothing about the result has to be saved.

Layout (168 bytes per deck, 6 decks per opponent, 1008 bytes per opponent, starting at ENEMY_DECK_BASE):
  0x00-0x77  3 Bakugan slots, 20-byte stride: 14-byte record + 6 zero bytes   (rest of the 120 bytes unused)
  0x78-0x83  3 gate cards,    4 bytes each (1 byte card id + 3 zero bytes)
  0x90-0x9B  3 ability cards, 4 bytes each
Deck 0-1 are the easy decks, 2-3 medium, 4-5 hard; the game moves to harder decks as the story progresses.
Record: byte 0 species, byte 4 attribute, bytes 5-6 G-Power (big endian), bytes 8-12 five movement stats (10x), byte 13 level.
"""
import random
from typing import Dict, List, Optional

from bakugan_data import VALID_COMBINED

ENEMY_DECK_BASE = 0x8033A793
ENEMY_STRIDE = 1008
DECK_STRIDE = 168
DECKS_PER_ENEMY = 6
ENEMY_COUNT = 16
BLOCK_LEN = ENEMY_COUNT * ENEMY_STRIDE      # 16128 bytes, Dan deck 1 through Marduk deck 6

BAKUGAN_SLOT_STRIDE = 20
RECORD_LEN = 14
GATE_OFFSET = 0x78
ABILITY_OFFSET = 0x90
CARD_STRIDE = 4

# G-Power ranges per enemy_stats option (0 default, 1 easy, 2 medium, 3 hard, 4 brawler)
GP_TIERS = {0: (120, 550), 1: (90, 350), 2: (150, 620), 3: (180, 700), 4: (210, 810)}
STAT_SHIFT = {0: 0, 1: -1, 2: 1, 3: 1, 4: 2}        # movement stat steps (1-5 scale) added per tier
# deck difficulty -> level range used when decks are generated
LEVEL_RANGES = {0: (1, 4), 1: (1, 4), 2: (4, 7), 3: (4, 7), 4: (7, 10), 5: (7, 10)}

NERF_ITEMS_TOTAL = 4          # the apworld puts 4 Enemy Difficulty Nerf items in the pool
NERF_GP_FRACTION = 0.5        # all nerfs together remove 50% of the seed's G-Power
NERF_STAT_POINTS = 5          # ...and 5 movement stat points per Bakugan


def deck_offset(enemy: int, deck: int) -> int:
    return enemy * ENEMY_STRIDE + deck * DECK_STRIDE


def _species_by_attribute() -> Dict[int, List[int]]:
    out: Dict[int, List[int]] = {}
    for combined in VALID_COMBINED:
        species, attr = divmod(combined, 6)
        out.setdefault(attr, []).append(species)
    return {a: sorted(s) for a, s in out.items()}


def baseline_looks_valid(block: bytes) -> bool:
    """True when the block holds real decks (Dan's first deck has a valid Bakugan), not zeros / a half-loaded game."""
    if len(block) != BLOCK_LEN:
        return False
    rec = block[0:RECORD_LEN]
    species, attr, level = rec[0], rec[4], rec[13]
    return species * 6 + attr in VALID_COMBINED and 1 <= level <= 10


def _gp_for_level(rng: random.Random, tier: int, level: int) -> int:
    """f = z + y * (x / 10) - u  with z = tier minimum, x = span, y = level, u random in 1..x/10."""
    z, top = GP_TIERS[tier]
    x = top - z
    step = max(1, x // 10)
    return max(1, z + level * step - rng.randint(1, step))


def _stat(rng: random.Random, level: int, tier: int) -> int:
    """Movement stat 1-5, weighted towards higher values for higher levels."""
    mean = 1 + 4 * (level - 1) / 9 + STAT_SHIFT[tier]
    return max(1, min(5, round(rng.gauss(mean, 1.0))))


def _random_deck(rng: random.Random, base: bytearray, deck: int, tier: int, by_attr: Dict[int, List[int]]) -> None:
    """Replace the Bakugan records and cards of one deck (in place). The opponent keeps each slot's attribute and the
    number of Bakugan the original deck had."""
    lo, hi = LEVEL_RANGES[deck]
    used = set()
    for slot in range(3):
        at = slot * BAKUGAN_SLOT_STRIDE
        old = base[at:at + RECORD_LEN]
        if old[13] == 0:
            continue                                  # empty slot in the original deck
        attr = old[4] if old[4] in by_attr else 0
        choices = [s for s in by_attr[attr] if s not in used] or by_attr[attr]
        species = rng.choice(choices)
        used.add(species)
        level = rng.randint(lo, hi)
        gp = _gp_for_level(rng, tier, level)
        stats = [_stat(rng, level, tier) * 10 for _ in range(5)]
        base[at:at + RECORD_LEN] = bytes([species, 0, 0, 0, attr]) + gp.to_bytes(2, "big") + bytes([0]) \
            + bytes(stats) + bytes([level])
    for offset, pool in ((GATE_OFFSET, range(0, 98)), (ABILITY_OFFSET, range(98, 191))):
        for i, card in enumerate(rng.sample(list(pool), 3)):
            base[offset + i * CARD_STRIDE] = card


def _scale_existing(base: bytearray, tier: int) -> None:
    """Randomization off: map the original G-Power range onto the tier's range and shift movement stats."""
    lo0, hi0 = GP_TIERS[0]
    lo, hi = GP_TIERS[tier]
    for slot in range(3):
        at = slot * BAKUGAN_SLOT_STRIDE
        if base[at + 13] == 0:
            continue
        gp = int.from_bytes(base[at + 5:at + 7], "big")
        frac = max(0.0, min(1.0, (gp - lo0) / (hi0 - lo0)))
        base[at + 5:at + 7] = round(lo + frac * (hi - lo)).to_bytes(2, "big")
        for i in range(8, 13):
            base[at + i] = max(10, min(50, base[at + i] + STAT_SHIFT[tier] * 10))


def _nerf(rng: random.Random, base: bytearray, nerfs: int) -> None:
    n = max(0, min(NERF_ITEMS_TOTAL, nerfs))
    if n == 0:
        return
    keep = 1 - NERF_GP_FRACTION * n / NERF_ITEMS_TOTAL
    points = round(NERF_STAT_POINTS * n / NERF_ITEMS_TOTAL)
    for slot in range(3):
        at = slot * BAKUGAN_SLOT_STRIDE
        if base[at + 13] == 0:
            continue
        gp = int.from_bytes(base[at + 5:at + 7], "big")
        base[at + 5:at + 7] = max(1, round(gp * keep)).to_bytes(2, "big")
        for _ in range(points):
            options = [i for i in range(8, 13) if base[at + i] > 10]
            if not options:
                break
            base[at + rng.choice(options)] -= 10


def needs_work(mode: int, tier: int, nerfs: int) -> bool:
    return mode != 0 or tier != 0 or nerfs > 0


def build_enemy_block(baseline: bytes, seed: int, mode: int, tier: int, nerfs: int) -> bytes:
    """The 16128 bytes that should be in memory. Bytes outside the Bakugan records and card ids are copied from the
    baseline untouched."""
    tier = tier if tier in GP_TIERS else 0
    by_attr = _species_by_attribute()
    out = bytearray(baseline)
    for enemy in range(ENEMY_COUNT):
        for deck in range(DECKS_PER_ENEMY):
            at = deck_offset(enemy, deck)
            view = bytearray(out[at:at + DECK_STRIDE])
            rng = random.Random(f"{seed}:{enemy}:{deck}")
            if mode != 0:
                _random_deck(rng, view, deck, tier, by_attr)
            elif tier != 0:
                _scale_existing(view, tier)
            _nerf(random.Random(f"{seed}:nerf:{enemy}:{deck}"), view, nerfs)
            out[at:at + DECK_STRIDE] = view
    return bytes(out)


def managed_ranges() -> List[range]:
    """Byte ranges inside the block that this module may change (used to compare memory with the expected block)."""
    out = []
    for enemy in range(ENEMY_COUNT):
        for deck in range(DECKS_PER_ENEMY):
            at = deck_offset(enemy, deck)
            for slot in range(3):
                out.append(range(at + slot * BAKUGAN_SLOT_STRIDE, at + slot * BAKUGAN_SLOT_STRIDE + RECORD_LEN))
            for off in (GATE_OFFSET, ABILITY_OFFSET):
                for i in range(3):
                    out.append(range(at + off + i * CARD_STRIDE, at + off + i * CARD_STRIDE + 1))
    return out


_RANGES: Optional[List[range]] = None


def matches(expected: bytes, current: bytes) -> bool:
    global _RANGES
    if _RANGES is None:
        _RANGES = managed_ranges()
    return all(expected[r.start:r.stop] == current[r.start:r.stop] for r in _RANGES)
