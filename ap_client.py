"""Minimal Archipelago protocol client (websocket + JSON), no CommonClient dependency.

Flow: RoomInfo -> (GetDataPackage) + Connect -> DataPackage -> Connected -> ReceivedItems...
Outgoing checks go out as LocationChecks.
"""
import asyncio
import json
import re
import uuid
from pathlib import Path

from constants import AP_VERSION, CLIENT_GOAL, GAME_NAME, ITEMS_HANDLING_ALL


class ApClient:
    def __init__(self, get_settings, log, on_change, cache_dir):
        self.get_settings = get_settings
        self.log = log
        self.on_change = on_change
        self.cache_dir = Path(cache_dir)
        self.on_connected = lambda: None  # set by the engine

        self.client_uuid = uuid.uuid4().hex
        self.games = {}          # game -> {item_ids, location_ids, item_names, location_names}
        self.status = "Disconnected"
        self.want = False        # True while the user wants to be connected (drives auto-reconnect)
        self.refused = False
        self.ws = None
        self._reset()

    def _reset(self):
        self.connected = False
        self.slot = None
        self.team = None
        self.seed_name = ""
        self.players = {}        # slot -> display name
        self.slot_game = {}      # slot -> game name
        self.checked = set()     # location ids the server (or we) know are checked
        self.missing = set()
        self.slot_data = {}
        self.items = []          # every NetworkItem received so far, in server order

    # ------------------------------------------------------------------ lookups
    @property
    def own_game(self) -> str:
        return self.slot_game.get(self.slot) or GAME_NAME

    @property
    def all_locations(self) -> set:
        """Every location that exists in this slot (checked or still missing)."""
        return self.checked | self.missing

    def item_name(self, item_id: int, slot=None) -> str:
        game = self.slot_game.get(self.slot if slot is None else slot)
        return self.games.get(game, {}).get("item_names", {}).get(item_id, f"Item #{item_id}")

    def own_location_id(self, name: str):
        return self.games.get(self.own_game, {}).get("location_ids", {}).get(name)

    def player_name(self, slot) -> str:
        return self.players.get(slot, f"Player {slot}")

    # ------------------------------------------------------------------ data package cache
    def _cache_path(self, game: str, checksum: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", game)
        return self.cache_dir / f"dp_{safe}_{checksum}.json"

    def _index_game(self, game: str, data: dict):
        items = data.get("item_name_to_id", {})
        locs = data.get("location_name_to_id", {})
        self.games[game] = {
            "item_ids": items,
            "location_ids": locs,
            "item_names": {v: k for k, v in items.items()},
            "location_names": {v: k for k, v in locs.items()},
        }

    def _load_cache(self, game: str, checksum: str):
        try:
            return json.loads(self._cache_path(game, checksum).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_cache(self, game: str, checksum, data: dict):
        if not checksum:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(game, checksum).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ transport
    async def _open(self, server: str):
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError("websockets is not installed (pip install websockets)") from e
        server = server.strip()
        urls = [server] if "://" in server else [f"ws://{server}", f"wss://{server}"]
        last = None
        for url in urls:
            try:
                return await websockets.connect(url, max_size=None, open_timeout=10)
            except Exception as e:  # try the next scheme
                last = e
        raise ConnectionError(f"could not connect to {server}: {last}")

    async def send(self, packets: list):
        if self.ws is not None:
            await self.ws.send(json.dumps(packets))

    async def _session(self):
        ws = await self._open(self.get_settings().server)
        self.ws = ws
        self.status = "Connected to server, logging in..."
        self.on_change()
        try:
            while True:
                raw = await ws.recv()
                for pkt in json.loads(raw):
                    await self.handle(pkt)
                    if self.refused:
                        return
        finally:
            self.ws = None
            try:
                await ws.close()
            except Exception:
                pass

    async def serve(self):
        """Stay connected (with auto-reconnect) until want is cleared or the task is cancelled."""
        self.want = True
        while self.want:
            self.status = "Connecting..."
            self.on_change()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"Archipelago connection lost: {e}")
            self._reset()
            if not self.want or self.refused:
                break
            self.status = "Reconnecting in 5s..."
            self.on_change()
            await asyncio.sleep(5)
        if not self.refused:
            self.status = "Disconnected"
        self.on_change()

    # ------------------------------------------------------------------ packet handling
    async def handle(self, pkt: dict):
        fn = getattr(self, f"_on_{pkt.get('cmd')}", None)
        if fn:
            await fn(pkt)

    async def _on_RoomInfo(self, pkt):
        self.seed_name = pkt.get("seed_name", "")
        checksums = pkt.get("datapackage_checksums") or {}
        need = []
        for game, checksum in checksums.items():
            cached = self._load_cache(game, checksum)
            if cached is not None:
                self._index_game(game, cached)
            else:
                need.append(game)
        out = []
        if need:
            out.append({"cmd": "GetDataPackage", "games": need})
        elif not checksums:
            out.append({"cmd": "GetDataPackage"})  # older servers: ask for everything
        s = self.get_settings()
        out.append({
            "cmd": "Connect",
            "password": s.password,
            "game": GAME_NAME,
            "name": s.slot,
            "uuid": self.client_uuid,
            "version": {"major": AP_VERSION[0], "minor": AP_VERSION[1], "build": AP_VERSION[2], "class": "Version"},
            "items_handling": ITEMS_HANDLING_ALL,
            "tags": [],
            "slot_data": True,
        })
        await self.send(out)

    async def _on_DataPackage(self, pkt):
        for game, data in pkt.get("data", {}).get("games", {}).items():
            self._save_cache(game, data.get("checksum"), data)
            self._index_game(game, data)

    async def _on_Connected(self, pkt):
        self.connected = True
        self.slot = pkt["slot"]
        self.team = pkt["team"]
        self.players = {
            p["slot"]: (p.get("alias") or p.get("name"))
            for p in pkt.get("players", []) if p.get("team") == self.team
        }
        self.slot_game = {int(k): v.get("game") for k, v in (pkt.get("slot_info") or {}).items()}
        self.slot_game.setdefault(self.slot, GAME_NAME)
        self.checked = set(pkt.get("checked_locations", []))
        self.missing = set(pkt.get("missing_locations", []))
        self.slot_data = pkt.get("slot_data") or {}
        self.items = []
        self.status = f"Connected as {self.player_name(self.slot)}"
        self.log(f"Connected to Archipelago as {self.player_name(self.slot)} "
                 f"({len(self.checked)} checked, {len(self.missing)} remaining)")
        self.on_connected()
        self.on_change()

    async def _on_ConnectionRefused(self, pkt):
        errors = ", ".join(pkt.get("errors", [])) or "unknown reason"
        self.log(f"Archipelago refused the connection: {errors}")
        self.status = f"Refused: {errors}"
        self.refused = True
        self.want = False
        self.on_change()

    async def _on_ReceivedItems(self, pkt):
        start, items = pkt.get("index", 0), pkt.get("items", [])
        if start == 0:
            self.items = list(items)           # full resync
        elif start == len(self.items):
            self.items.extend(items)           # normal incremental update
        else:
            self.log("Item list out of sync, requesting a resync")
            await self.send([{"cmd": "Sync"}])
            return
        self.on_change()

    async def _on_RoomUpdate(self, pkt):
        if "checked_locations" in pkt:
            self.checked.update(pkt["checked_locations"])
            self.missing.difference_update(pkt["checked_locations"])
        self.on_change()

    async def _on_PrintJSON(self, pkt):
        text = self._render(pkt)
        if text.strip():
            self.log(text)

    async def _on_InvalidPacket(self, pkt):
        self.log(f"Server rejected a packet: {pkt.get('text', pkt)}")

    def _render(self, pkt) -> str:
        out = []
        for part in pkt.get("data", []):
            kind, text = part.get("type"), str(part.get("text", ""))
            try:
                if kind == "player_id":
                    text = self.player_name(int(text))
                elif kind == "item_id":
                    text = self.item_name(int(text), part.get("player"))
                elif kind == "location_id":
                    game = self.slot_game.get(part.get("player"))
                    text = self.games.get(game, {}).get("location_names", {}).get(int(text), text)
            except ValueError:
                pass
            out.append(text)
        return "".join(out)

    # ------------------------------------------------------------------ outgoing
    async def send_checks(self, location_ids):
        ids = sorted(set(location_ids))
        if not ids:
            return
        await self.send([{"cmd": "LocationChecks", "locations": ids}])
        self.checked.update(ids)
        self.missing.difference_update(ids)

    async def send_goal(self):
        await self.send([{"cmd": "StatusUpdate", "status": CLIENT_GOAL}])
