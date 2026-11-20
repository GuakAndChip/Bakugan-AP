"""Loads bakugan_hex_map_fixed.json: per (species, attribute) memory offset and 14-byte base stat record.

The exact schema isn't pinned down yet, so the loader is tolerant: it walks the whole JSON and treats
any object with a `species_idx` and an attribute field as one Bakugan entry.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ATTR_KEYS = ("attribute_idx", "attribute")
_OFFSET_KEYS = ("dec_offset", "offset", "hex_offset", "address")
_PAYLOAD_KEYS = ("base_stats_hex", "payload", "hex", "stats_hex")


@dataclass
class HexEntry:
    offset: Optional[int]            # from the Dragonoid anchor; None = use combined_id * 20
    payload: Optional[bytes] = None  # 14-byte base record


def parse_hex_bytes(value) -> bytes:
    if isinstance(value, (list, tuple)):
        return bytes(value)
    text = str(value).lower().replace("0x", "")
    for ch in " ,_-":
        text = text.replace(ch, "")
    return bytes.fromhex(text)


def _parse_index(v) -> int:
    return v if isinstance(v, int) else int(str(v).strip(), 0)


def _parse_offset(key: str, v) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if key == "hex_offset" and not s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 0)


def _walk(obj, out: Dict[int, HexEntry], warnings: List[str]):
    if isinstance(obj, dict):
        attr_key = next((k for k in _ATTR_KEYS if k in obj), None)
        if "species_idx" in obj and attr_key:
            try:
                species, attr = _parse_index(obj["species_idx"]), _parse_index(obj[attr_key])
                off_key = next((k for k in _OFFSET_KEYS if k in obj), None)
                pay_key = next((k for k in _PAYLOAD_KEYS if k in obj), None)
                payload = parse_hex_bytes(obj[pay_key]) if pay_key and obj[pay_key] else None
                if payload is not None:
                    payload = bytearray(payload)
                    payload[0] = species   # byte 0 is built from species_idx, not trusted from the file
                    payload = bytes(payload)
                offset = _parse_offset(off_key, obj[off_key]) if off_key else None
                out[6 * species + attr] = HexEntry(offset, payload)
            except (ValueError, TypeError, IndexError) as e:
                warnings.append(f"hex map entry skipped ({e}): {str(obj)[:80]}")
            return
        for v in obj.values():
            _walk(v, out, warnings)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out, warnings)


def load_hex_map(path: Path) -> Tuple[Dict[int, HexEntry], List[str]]:
    """Returns ({combined_id: entry}, warnings). Raises OSError/ValueError on an unreadable file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: Dict[int, HexEntry] = {}
    warnings: List[str] = []
    _walk(raw, out, warnings)
    if not out:
        raise ValueError("no Bakugan entries found (expected objects with species_idx and an attribute field)")
    return out, warnings
