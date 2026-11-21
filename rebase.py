"""Memory wrapper that applies the per-machine offsets found by locator.py.

Everything in the client uses the addresses from constants.py ("logical" addresses). This layer shifts the ones that
live in the save-data window (MEM2) and the enemy-deck window (MEM1) by the deltas found for this machine, and leaves
everything else (game id, player name) alone. Until a delta is known, access to those windows is refused, so nothing
can be read or written at a wrong place."""
from typing import Optional

from locator import ENEMY_WINDOW, MEM2_WINDOW
from memory import MemoryAccessError


def parse_delta(text: str) -> Optional[int]:
    """'' -> None (auto). '0x1230', '1230', '-0x10' (always hexadecimal) -> int."""
    t = (text or "").strip().lower()
    if not t:
        return None
    neg = t.startswith("-")
    t = t.lstrip("+-").replace("0x", "")
    value = int(t, 16)
    return -value if neg else value


class RebasedMemory:
    def __init__(self, inner):
        self._inner = inner
        self.mem2_delta: Optional[int] = None
        self.enemy_delta: Optional[int] = None

    def __getattr__(self, name):          # hook, unhook, is_hooked, batch, ...
        return getattr(self._inner, name)

    def real(self, address: int) -> int:
        """The address in Dolphin for a constants.py address (raises if the offset isn't known yet)."""
        if MEM2_WINDOW[0] <= address < MEM2_WINDOW[1]:
            if self.mem2_delta is None:
                raise MemoryAccessError("The game's save data hasn't been located yet")
            return address + self.mem2_delta
        if ENEMY_WINDOW[0] <= address < ENEMY_WINDOW[1]:
            if self.enemy_delta is None:
                raise MemoryAccessError("The enemy decks haven't been located yet")
            return address + self.enemy_delta
        return address

    def read(self, address: int, size: int) -> bytes:
        return self._inner.read(self.real(address), size)

    def write(self, address: int, data: bytes) -> None:
        self._inner.write(self.real(address), data)
