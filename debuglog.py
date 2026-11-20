"""Debug helpers: a memory wrapper that logs the bytes before and after every write."""
from contextlib import contextmanager
from typing import Callable, Optional

SHOW_BYTES = 48          # longest run of bytes printed for one write
BATCH_LINES = 40         # in a batch (refresh, startup item sync, ...) only this many writes are printed in full


def hex_str(data: Optional[bytes], limit: int = SHOW_BYTES) -> str:
    if data is None:
        return "<unreadable>"
    shown = " ".join(f"{b:02X}" for b in data[:limit])
    return shown + (f" ... (+{len(data) - limit} more)" if len(data) > limit else "")


class DebugMemory:
    """Wraps a memory backend. Everything is passed through; write() also logs
    `WRITE 0xADDR [n bytes] before=... after=...` (after is read back, so it shows what really landed)."""

    def __init__(self, inner, emit: Callable[[str], None], enabled: Callable[[], bool]):
        self._inner = inner
        self._emit = emit
        self._enabled = enabled
        self._batch = None            # [label, writes, bytes, printed]

    def __getattr__(self, name):      # hook, unhook, read, is_hooked, ...
        return getattr(self._inner, name)

    @contextmanager
    def batch(self, label: str):
        """Group many writes: the first BATCH_LINES are printed in full, the rest are only counted."""
        if self._batch is not None:   # nested: join the outer batch
            yield
            return
        self._batch = [label, 0, 0, 0]
        try:
            yield
        finally:
            _label, writes, total, printed = self._batch
            self._batch = None
            if self._enabled() and writes > printed:
                self._emit(f"WRITE [{label}] {writes - printed} more write(s) not printed ({writes} total, {total} bytes)")

    def write(self, address: int, data: bytes) -> None:
        if not self._enabled():
            self._inner.write(address, data)
            return
        before = self._try_read(address, len(data))
        self._inner.write(address, data)
        after = self._try_read(address, len(data))
        status = "ok" if after == data else "MISMATCH"
        if self._batch is not None:
            self._batch[1] += 1
            self._batch[2] += len(data)
            if self._batch[3] >= BATCH_LINES:
                return
            self._batch[3] += 1
        self._emit(f"WRITE 0x{address:08X} [{len(data)}] {status} before={hex_str(before)} after={hex_str(after)}")

    def _try_read(self, address: int, size: int) -> Optional[bytes]:
        try:
            return self._inner.read(address, size)
        except Exception:
            return None
