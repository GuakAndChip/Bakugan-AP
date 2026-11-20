"""Turns received Archipelago items into memory writes (Bakugan records, cards, BP, rank points, decks)."""
from typing import Dict, Tuple

from bakugan_data import VALID_COMBINED
from constants import (BAKUGAN_COMBINED_COUNT, BAKUGAN_ITEM_BASE, BAKUGAN_RECORD_LEN, BAKUGAN_RECORD_STRIDE,
                       BP_ADDR, BP_ITEMS, GPOWER_BOOST, GPOWER_ITEM, GPOWER_MAX, ENEMY_NERF_ITEM, STAT_BOOST_ITEM, STAT_MAX, STAT_UNIT, BP_MAX, CARD_BASE, CARD_COUNT, CARD_ITEM_BASE, DECK1_ADDR, DECK2_ADDR,
                       RANK_MAX, RANK_POINT_ITEMS, RANK_POINTS_ADDR, ROOT_BAKUGAN_ANCHOR)
from hexmap import HexEntry
from memory import MemoryAccessError

DECK_ADDRS = (DECK1_ADDR, DECK2_ADDR)
EMPTY_SLOT = b"\xff\xff"
IDEMPOTENT = ("bakugan", "card")        # safe to write again
ADDITIVE = ("rank", "money")            # must only ever be applied once
BOOSTS = ("gpboost", "statboost")       # also applied once; remembered per Bakugan so rewrites keep them


class InjectionError(RuntimeError):
    """An item can't be written because of configuration or a failed read-back."""


def classify(item_id: int) -> Tuple[str, int]:
    """(kind, argument): bakugan -> combined id, card -> memory index, rank/money -> amount."""
    combined = item_id - BAKUGAN_ITEM_BASE
    if 0 <= combined < BAKUGAN_COMBINED_COUNT:
        return "bakugan", combined
    card = item_id - CARD_ITEM_BASE
    if 0 <= card < CARD_COUNT:
        return "card", card
    if item_id == GPOWER_ITEM:
        return "gpboost", GPOWER_BOOST
    if item_id == ENEMY_NERF_ITEM:
        return "nerf", 1
    if item_id == STAT_BOOST_ITEM:
        return "statboost", 1
    if item_id in RANK_POINT_ITEMS:
        return "rank", RANK_POINT_ITEMS[item_id]
    if item_id in BP_ITEMS:
        return "money", BP_ITEMS[item_id]
    return "unsupported", 0             # stat boosters, enemy nerfs: not implemented yet


def bakugan_name(combined: int) -> str:
    return VALID_COMBINED.get(combined, f"Bakugan #{combined}")


class ItemInjector:
    def __init__(self, mem):
        self.mem = mem
        self.hex_map: Dict[int, HexEntry] = {}
        self.boosts: Dict[int, list] = {}   # combined id -> [extra G-Power, extra stat steps]

    # ---------------------------------------------------------------- Bakugan
    def _bakugan_target(self, combined: int) -> Tuple[int, bytes]:
        entry = self.hex_map.get(combined)
        if entry is None or entry.payload is None:
            if not self.hex_map:
                why = "the hex map is not loaded (see the Log tab for 'Could not load Bakugan hex map')"
            else:
                why = (f"the loaded hex map has {len(self.hex_map)} entries but none for combined id {combined} "
                       f"(species {combined // 6}, attribute {combined % 6}) - it may be an older/different file")
            raise InjectionError(f"No base stats for {bakugan_name(combined)}: {why}")
        offset = entry.offset if entry.offset is not None else combined * BAKUGAN_RECORD_STRIDE
        return ROOT_BAKUGAN_ANCHOR + offset, self._boosted(combined, entry.payload)

    # ---------------------------------------------------------------- stat boosters
    @staticmethod
    def _apply_boost(payload: bytes, gp_add: int, stat_steps: int) -> bytes:
        """Record layout: bytes 5-6 G-Power (big endian), bytes 8-12 five movement stats (10x values)."""
        rec = bytearray(payload)
        gp = min(GPOWER_MAX, int.from_bytes(rec[5:7], "big") + gp_add)
        rec[5:7] = gp.to_bytes(2, "big")
        for i in range(8, 13):
            rec[i] = min(STAT_MAX, rec[i] + stat_steps * STAT_UNIT)
        return bytes(rec)

    def _boosted(self, combined: int, payload: bytes) -> bytes:
        gp_add, steps = self.boosts.get(combined, (0, 0))
        return self._apply_boost(payload, gp_add, steps) if (gp_add or steps) else payload

    def boost_room(self, combined: int, kind: str) -> bool:
        """True if this Bakugan can still take a booster of this kind without hitting its cap."""
        entry = self.hex_map.get(combined)
        if entry is None or entry.payload is None:
            return False
        rec = self._boosted(combined, entry.payload)
        if kind == "gpboost":
            return int.from_bytes(rec[5:7], "big") < GPOWER_MAX
        return any(rec[i] < STAT_MAX for i in range(8, 13))

    def add_boost(self, combined: int, kind: str, amount: int) -> None:
        """Remember the booster for this Bakugan and rewrite its record with it applied."""
        cur = list(self.boosts.get(combined, (0, 0)))
        cur[0 if kind == "gpboost" else 1] += amount
        self._bakugan_target(combined)          # fail early (before remembering) if there are no base stats
        before = self.boosts.get(combined)
        self.boosts[combined] = cur
        try:
            self.write_bakugan(combined)
        except Exception:
            if before is None:                   # a failed write must not leave the booster half-counted
                self.boosts.pop(combined, None)
            else:
                self.boosts[combined] = before
            raise

    def write_bakugan(self, combined: int) -> None:
        addr, payload = self._bakugan_target(combined)
        self.mem.write(addr, payload)
        if self.mem.read(addr, len(payload)) != payload:
            raise InjectionError(f"Read-back mismatch writing {bakugan_name(combined)} at 0x{addr:08X}")

    def bakugan_present(self, combined: int) -> bool:
        addr, payload = self._bakugan_target(combined)
        record = self.mem.read(addr, BAKUGAN_RECORD_LEN)
        species, attr = divmod(combined, 6)
        # Pyrus Dragonoid is species 0 / attribute 0, so also require a non-zero level byte.
        return record[0] == species and record[4] == attr and record[13] != 0

    # ---------------------------------------------------------------- cards
    def write_card(self, index: int) -> None:
        self.mem.write(CARD_BASE + index, b"\x01")
        if self.mem.read(CARD_BASE + index, 1) != b"\x01":
            raise InjectionError(f"Read-back mismatch writing card {index}")

    def card_present(self, index: int) -> bool:
        return self.mem.read(CARD_BASE + index, 1) == b"\x01"

    # ---------------------------------------------------------------- BP / rank points
    def add_rank_points(self, amount: int) -> None:
        self._add(RANK_POINTS_ADDR, amount, RANK_MAX)

    def add_money(self, amount: int) -> None:
        self._add(BP_ADDR, amount, BP_MAX)

    def _add(self, addr: int, amount: int, cap: int) -> None:
        current = int.from_bytes(self.mem.read(addr, 4), "big")
        self.mem.write(addr, min(cap, current + amount).to_bytes(4, "big"))

    # ---------------------------------------------------------------- inventory wipe
    def wipe_inventory(self) -> None:
        """Clears every valid Bakugan record slot and the whole card block."""
        for combined in VALID_COMBINED:
            entry = self.hex_map.get(combined)
            offset = entry.offset if entry and entry.offset is not None else combined * BAKUGAN_RECORD_STRIDE
            self.mem.write(ROOT_BAKUGAN_ANCHOR + offset, bytes(BAKUGAN_RECORD_LEN))
        self.mem.write(CARD_BASE, bytes(CARD_COUNT))

    # ---------------------------------------------------------------- decks
    @staticmethod
    def deck_bytes(deck: dict) -> bytes:
        """36 bytes: 3 groups of (3 active 2-byte ids + 3 x 0xFFFF): Bakugan, gate cards, ability cards."""
        out = b""
        for key, valid, label in (
                ("bakugan", lambda i: i in VALID_COMBINED, "a valid Bakugan id"),
                ("gate_cards", lambda i: 0 <= i <= 97, "a gate card (0-97)"),
                ("ability_cards", lambda i: 98 <= i <= 190, "an ability card (98-190)")):
            ids = list(deck.get(key, []))
            if len(ids) > 3:
                raise InjectionError(f"Deck {key} has {len(ids)} entries; at most 3 fit")
            for i in ids:
                if not isinstance(i, int) or isinstance(i, bool) or not valid(i):
                    raise InjectionError(f"Deck {key} entry {i!r} is not {label}")
            out += b"".join(i.to_bytes(2, "big") for i in ids) + EMPTY_SLOT * (6 - len(ids))
        return out

    def write_deck(self, slot: int, deck: dict) -> None:
        data = self.deck_bytes(deck)
        self.mem.write(DECK_ADDRS[slot], data)
        if self.mem.read(DECK_ADDRS[slot], len(data)) != data:
            raise InjectionError(f"Read-back mismatch writing deck {slot + 1}")
