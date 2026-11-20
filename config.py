"""User settings and the small persistent state file."""
import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def app_dir() -> Path:
    """Folder for settings, state and the Archipelago data-package cache."""
    d = Path(os.environ.get("BAKUGAN_AP_HOME", Path.home() / ".bakugan_ap_client"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Settings:
    server: str = "archipelago.gg:38281"
    slot: str = ""
    password: str = ""
    poll_interval: float = 0.25                # seconds between Dolphin reads
    hex_map_path: str = "bakugan_hex_map_fixed.json"  # Bakugan offsets + base stat records
    debug_log_writes: bool = True              # log the bytes before/after every memory write (Log tab + client.log)
    enemy_decks_enabled: bool = False          # EXPERIMENTAL: rewrite enemy decks in MEM1 (off until verified)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_dir() / "settings.json"
        s = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(cls)}
                for k, v in raw.items():
                    if k in known:
                        setattr(s, k, v)
            except (OSError, ValueError):
                pass  # a corrupt settings file just falls back to defaults
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or app_dir() / "settings.json"
        _atomic_write(path, json.dumps(asdict(self), indent=2))

    def resolve_hex_map_path(self) -> Path:
        p = Path(self.hex_map_path)
        if p.is_absolute():
            return p
        bases = [Path.cwd(), Path(__file__).resolve().parent, app_dir()]
        if getattr(sys, "_MEIPASS", None):           # files bundled into the PyInstaller exe
            bases.insert(0, Path(sys._MEIPASS))
        for base in bases:
            for candidate in (base / p, base / "data" / p):
                if candidate.exists():
                    return candidate
        # exact name not found: accept any "*hex_map*.json" (e.g. a browser-renamed download)
        for base in bases:
            for folder in (base, base / "data"):
                try:
                    found = sorted(folder.glob("*hex_map*.json")) if folder.is_dir() else []
                except OSError:
                    found = []
                found = [f for f in found if "card" not in f.name.lower()]
                if found:
                    return found[0]
        return p

    def hex_map_search_dirs(self) -> list[Path]:
        bases = [Path.cwd(), Path(__file__).resolve().parent, app_dir()]
        if getattr(sys, "_MEIPASS", None):
            bases.insert(0, Path(sys._MEIPASS))
        return bases


class StateStore:
    """Per seed/slot values that must survive restarts: how many received items were already applied
    (so BP/rank point adds never happen twice), peak BP, and whether the starter decks were written."""

    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "state.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get_value(self, key: str, name: str, default=0):
        return self.data.get(key, {}).get(name, default)

    def set_value(self, key: str, name: str, value) -> None:
        self.data.setdefault(key, {})[name] = value
        _atomic_write(self.path, json.dumps(self.data, indent=2))
