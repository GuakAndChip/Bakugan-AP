"""The brain: polls Dolphin, tracks stats, sends checks and the goal, applies received items.

Runs on its own thread with an asyncio loop so the GUI never blocks. The GUI talks to it through
thread-safe methods and reads results from `engine.events` (log lines and state snapshots).
"""
import asyncio
import queue
import random
import threading
import time

from ap_client import ApClient
from checks import achieved_locations, goal_reached
from config import Settings, StateStore, app_dir
from constants import (CARD_BASE, CARD_COUNT, DECK1_ADDR, DECK2_ADDR, ECON_BLOCK_LEN, MAX_DEFEATS, OPPONENTS,
                       PLAYER_NAME_ADDR, RANK_POINTS_ADDR, STATS_BLOCK_ADDR, STATS_BLOCK_LEN, defeat_location_id)
import enemy_decks
from debuglog import DebugMemory, hex_str
import locator
from rebase import RebasedMemory, parse_delta
from gamestate import GameState, read_game_state
from hexmap import load_hex_map
from items import ADDITIVE, BOOSTS, InjectionError, ItemInjector, bakugan_name, classify
from memory import DolphinMemory, MemoryAccessError


class Engine:
    def __init__(self, settings: Settings, memory=None, state: StateStore | None = None):
        self.settings = settings
        # layers: Dolphin <- debug logging (real addresses) <- rebasing (documented addresses -> this machine's)
        self.raw = DebugMemory(memory or DolphinMemory(), self._debug, lambda: bool(self.settings.debug_log_writes))
        self.rebase = RebasedMemory(self.raw)
        self.mem = self.rebase
        self.state = state or StateStore()
        self.inj = ItemInjector(self.mem)
        self.events: "queue.Queue" = queue.Queue()
        self.ap = ApClient(lambda: self.settings, self.log, self.publish, app_dir() / "datapackage")
        self.ap.on_connected = self._on_ap_connected

        self.loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = False
        self._ap_task = None

        self.dolphin_status = "Dolphin not found"
        self.player_name = ""
        self.gs: GameState | None = None    # latest game state, None while Dolphin/game is unavailable
        self.session_wins = 0
        self.started = time.time()

        # Per seed/slot progress (persisted in StateStore under self.key)
        self.key = ""
        self.applied = 0                    # received items already written/handled, in server order
        self.additive_through = 0           # BP / rank items below this index were already added
        self.peak_money = 0                 # highest BP ever seen (for the Money Bags checks)
        self.decks_written = False
        self.goal_sent = False
        self.item_status = {}
        self.enemy_baseline: bytes | None = None   # original enemy decks, read once per seed before any write
        self._enemy_expected: tuple | None = None  # (params, block) cache
        self._last_enemy_check = 0.0
        self._enemy_off_logged = False
        self._enemy_wait_logged = None
        self.banked_boosts: dict = {}       # boosters received while no Bakugan could take them yet

        self.rebase.mem2_delta = self._manual_delta(self.settings.mem2_delta_override)   # None = find it
        self.rebase.enemy_delta = self._manual_delta(self.settings.enemy_delta_override)
        self._last_locate = 0.0
        self._last_verify = 0.0
        self._verify_fail = 0
        self._last_enemy_locate = 0.0
        self._manual_logged = False

        self._last_hook_try = 0.0
        self._last_name_read = 0.0
        self._last_tick_err = None
        self._last_inject_err = None
        self._decks_wait_logged = False

    # ------------------------------------------------------------------ plumbing
    def log(self, text: str):
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.events.put(("log", line))
        self._append_log_file(line)

    def _debug(self, text: str):
        self.log(f"[debug] {text}")

    def _append_log_file(self, line: str):
        """Everything shown in the Log tab is also kept in client.log (the tab only holds the latest lines)."""
        try:
            path = app_dir() / "client.log"
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def start(self):
        self.reload_hex_map()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_until_complete(self._main())
        finally:
            self.loop.close()

    def submit(self, coro):
        """Run a coroutine on the engine thread (called from the GUI thread)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        try:
            if self.loop and self.loop.is_running():
                self.submit(self.disconnect_ap()).result(timeout=3)
        except Exception:
            pass
        self._stop = True

    async def _main(self):
        self.log("Client started")
        while not self._stop:
            try:
                await self.tick()
                self._last_tick_err = None
            except Exception as e:  # keep the loop alive, but don't spam the same error
                msg = f"Internal error: {e!r}"
                if msg != self._last_tick_err:
                    self.log(msg)
                    self._last_tick_err = msg
            await asyncio.sleep(max(0.05, float(self.settings.poll_interval)))

    def reload_hex_map(self):
        path = self.settings.resolve_hex_map_path()
        try:
            self.inj.hex_map, warnings = load_hex_map(path)
        except (OSError, ValueError) as e:
            self.inj.hex_map = {}
            where = ", ".join(str(d) for d in self.settings.hex_map_search_dirs())
            self.log(f"Could not load Bakugan hex map '{path}': {e}. Looked in: {where}")
            return
        for w in warnings:
            self.log(w)
        n_stats = sum(1 for x in self.inj.hex_map.values() if x.payload)
        self.log(f"Loaded {len(self.inj.hex_map)} Bakugan ({n_stats} with base stats) from {path.resolve()}")

    # ------------------------------------------------------------------ Archipelago control
    async def connect_ap(self):
        await self.disconnect_ap()
        self.ap.refused = False
        self._ap_task = asyncio.ensure_future(self.ap.serve())

    async def disconnect_ap(self):
        task, self._ap_task = self._ap_task, None
        self.ap.want = False
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self.ap._reset()
        self.ap.status = "Disconnected"
        self.publish()

    def _on_ap_connected(self):
        self.key = f"{self.ap.seed_name}:{self.ap.team}:{self.ap.slot}"
        self.applied = int(self.state.get_value(self.key, "items_applied", 0))
        self.additive_through = int(self.state.get_value(self.key, "additive_through", 0))
        self.peak_money = int(self.state.get_value(self.key, "peak_money", 0))
        self.decks_written = bool(self.state.get_value(self.key, "decks_written", 0))
        self.inj.boosts = {int(c): list(v) for c, v in self.state.get_value(self.key, "boosts", {}).items()}
        base_hex = self.state.get_value(self.key, "enemy_baseline", "")
        self.enemy_baseline = bytes.fromhex(base_hex) if base_hex else None
        self._enemy_expected = None
        self._enemy_wait_logged = None
        self.banked_boosts = {k: int(v) for k, v in self.state.get_value(self.key, "banked_boosts", {}).items()}
        self.item_status = {i: "done" for i in range(self.applied)}
        self.goal_sent = False
        self._last_inject_err = None
        self._decks_wait_logged = False

    def _save(self, name: str, value):
        if self.key:
            self.state.set_value(self.key, name, value)

    # ------------------------------------------------------------------ main tick
    async def tick(self):
        self._poll_dolphin()
        if self.gs is not None:
            if self.gs.money is not None and self.gs.money > self.peak_money:
                self.peak_money = self.gs.money
                self._save("peak_money", self.peak_money)
            await self._send_new_checks()
            await self._maybe_send_goal()
            self._process_items()
            self._maintain_enemy_decks()
            if self.banked_boosts and self.applied >= len(self.ap.items):
                try:
                    self._spend_banked_boosts()
                except (MemoryAccessError, InjectionError) as ex:
                    self.log(f"Couldn't apply a banked booster: {ex}")
            self._maybe_write_decks()
        self.publish()

    def _poll_dolphin(self):
        try:
            if not self.mem.is_hooked():
                if time.time() - self._last_hook_try < 1.0:
                    self.gs = None
                    return
                self._last_hook_try = time.time()
                if not self.mem.hook():
                    self.dolphin_status = "Dolphin not found"
                    self.gs = None
                    return
                self.log("Hooked to Dolphin")
                self.rebase.mem2_delta = None
                self.rebase.enemy_delta = None
                self._verify_fail = 0
                try:
                    gid = self.mem.read(0x80000000, 6).decode("ascii", "replace")
                    self.log(f"Game ID: {gid}")      # region / version of the game (addresses differ between versions)
                except (MemoryAccessError, ValueError):
                    pass
            if not self._ensure_root():
                self.dolphin_status = "Dolphin found, looking for the game's data"
                self.gs = None
                return
            new = read_game_state(self.mem)
        except MemoryAccessError as e:
            text = str(e)
            blocking = ("Windows is required", "couldn't open")
            self.dolphin_status = text if any(b in text for b in blocking) else "Dolphin found, waiting for the game"
            self.gs = None
            self.mem.unhook()   # re-hook next time in case Dolphin was restarted
            return
        d = self.rebase.mem2_delta
        self.dolphin_status = "Dolphin connected" + (f" (data offset {d:+#x})" if d else "")
        self._track_wins(self.gs, new)
        self.gs = new
        if time.time() - self._last_name_read > 8:
            self._last_name_read = time.time()
            self._read_player_name()

    # ------------------------------------------------------------------ finding the game's data
    def _manual_delta(self, text: str):
        try:
            return parse_delta(text)
        except ValueError:
            self.log(f"Ignoring offset override '{text}' (use hex, like 0x1230)")
            return None

    def _ensure_root(self) -> bool:
        """True when the save-data offset is known and still checks out. May take a few seconds when it has to scan."""
        r, now = self.rebase, time.time()
        manual = self._manual_delta(self.settings.mem2_delta_override)
        if manual is not None:
            if r.mem2_delta != manual:
                r.mem2_delta = manual
                self.log(f"Using the save-data offset from Settings: {manual:+#x}")
            return True
        if r.mem2_delta is not None:
            if now - self._last_verify < 2.0:
                return True
            self._last_verify = now
            if locator.check_mem2(self.raw.read, r.mem2_delta):
                self._verify_fail = 0
                return True
            self._verify_fail += 1
            if self._verify_fail < 3:
                return True
            self.log("The game's data no longer sits where it was (another save loaded?), searching again")
            r.mem2_delta = None
        if now - self._last_locate < 5.0:
            return False
        self._last_locate = now
        return self._locate_mem2()

    def _locate_mem2(self) -> bool:
        hints = [self.state.get_value("_locator", "mem2_delta", 0) or 0, 0]
        self.log("Searching Dolphin memory for the game's save data (a few seconds)...")
        cands = locator.find_mem2(self.raw.read, hints=hints)
        pick = locator.best(cands)
        if pick is None:
            if cands:
                self.log("Several places look like the game's data, can't tell which is live. Candidates:")
                for c in cands[:5]:
                    self.log(f"  offset {c.delta:+#x}  score {c.score}  rank points {c.rank}  BP {c.bp}")
                self.log("Pick the one whose BP matches the game and enter its offset in Settings (save-data offset)")
            else:
                self.log("Game data not found yet. Load a save in the game (the client keeps looking)")
            return False
        self.rebase.mem2_delta = pick.delta
        self._verify_fail = 0
        self.state.set_value("_locator", "mem2_delta", pick.delta)
        self.log(f"Found the game's save data: offset {pick.delta:+#x} from the documented addresses "
                 f"(deck 1 at 0x{DECK1_ADDR + pick.delta:08X}, rank points {pick.rank}, BP {pick.bp})")
        return True

    def _ensure_enemy_root(self) -> bool:
        r, now = self.rebase, time.time()
        manual = self._manual_delta(self.settings.enemy_delta_override)
        if manual is not None:
            r.enemy_delta = manual
            return True
        if r.enemy_delta is not None:
            return True
        if now - self._last_enemy_locate < 10.0:
            return False
        self._last_enemy_locate = now
        hints = [self.state.get_value("_locator", "enemy_delta", 0) or 0, 0]
        found = locator.find_enemy(self.raw.read, hints=hints)
        if not found:
            self.log("Enemy decks not found in memory yet (wrong game version, or the game hasn't loaded them)")
            return False
        r.enemy_delta = found[0]
        self.state.set_value("_locator", "enemy_delta", found[0])
        self.log(f"Found the enemy decks: offset {found[0]:+#x}" + (f" (also valid: {[hex(x) for x in found[1:4]]})" if len(found) > 1 else ""))
        return True

    async def relocate(self):
        """Button: forget the offsets and search again now."""
        self.rebase.mem2_delta = None
        self.rebase.enemy_delta = None
        self.enemy_baseline_checked = False
        self._last_locate = self._last_enemy_locate = 0.0
        if not self.mem.is_hooked() and not self.mem.hook():
            self.log("Dolphin not found")
            return
        if self._ensure_root():
            if self.settings.enemy_decks_enabled:
                self._ensure_enemy_root()

    def real_address(self, address: int) -> int:
        try:
            return self.rebase.real(address)
        except MemoryAccessError:
            return address

    def _read_player_name(self):
        try:
            raw = self.mem.read(PLAYER_NAME_ADDR, 16)
        except MemoryAccessError:
            return
        text = raw.split(b"\x00", 1)[0].decode("latin-1")
        self.player_name = text if text.isprintable() else ""

    def _track_wins(self, prev: GameState | None, new: GameState):
        if prev is None:
            return  # first read after hooking: just a baseline
        rises = [(i, a, b) for i, (a, b) in enumerate(zip(prev.wins, new.wins)) if b > a]
        drops = [i for i, (a, b) in enumerate(zip(prev.wins, new.wins)) if b < a]
        if drops or sum(b - a for _, a, b in rises) > 2:
            # Too big to be real fights: a save was loaded. Resync without counting session wins.
            self.log("Win counts changed a lot at once (save loaded?), resynced")
            return
        for i, a, b in rises:
            for n in range(a + 1, b + 1):
                self.log(f"Won against {OPPONENTS[i]} (win #{n}), total {sum(new.wins)}")
            self.session_wins += b - a

    # ------------------------------------------------------------------ checks and goal
    def cap(self):
        """(defeat checks that exist per opponent, where that number came from)."""
        from_slot = self.ap.slot_data.get("enemy_defeats") if self.ap.connected else None
        if isinstance(from_slot, int) and 1 <= from_slot <= MAX_DEFEATS:
            return from_slot, "slot data"
        return MAX_DEFEATS, "default"

    def _counts(self):
        kinds = [classify(it["item"])[0] for it in self.ap.items]
        return kinds.count("bakugan"), kinds.count("card")

    def _location_name(self, loc_id: int) -> str:
        return self.ap.games.get(self.ap.own_game, {}).get("location_names", {}).get(loc_id, f"location #{loc_id}")

    async def _send_new_checks(self):
        if not self.ap.connected:
            return
        bakugan, cards = self._counts()
        sd = self.ap.slot_data
        legacy = int(sd.get("collector_threshold", 40))          # older seeds: one fixed number for both
        b_target = int(sd.get("collector_bakugan_target", legacy))
        c_target = int(sd.get("collector_card_target", legacy))
        earned = achieved_locations(self.gs, self.cap()[0], b_target, c_target, bakugan, cards, self.peak_money)
        new = sorted(loc for loc in earned if loc in self.ap.missing)   # only locations that exist and are open
        if not new:
            return
        await self.ap.send_checks(new)
        if len(new) <= 4:
            for loc in new:
                self.log(f"Sent check: {self._location_name(loc)}")
        else:
            self.log(f"Sent {len(new)} checks")

    async def _maybe_send_goal(self):
        if self.goal_sent or not self.ap.connected:
            return
        if goal_reached(self.gs, self.ap.slot_data):
            await self.ap.send_goal()
            self.goal_sent = True
            self.log("Goal complete! Reported to Archipelago")
            self._goal_report()

    def _goal_report(self):
        """One-off dump when the goal is reached: game state, what the client believes it did, and raw memory."""
        lines = ["===== GOAL REPORT ====="]
        try:
            gs = self.gs
            lines.append(f"Slot key: {self.key}")
            lines.append(f"Offsets: save data {self.rebase.mem2_delta}, enemy decks {self.rebase.enemy_delta}")
            try:
                lines.append("Game ID: " + self.mem.read(0x80000000, 6).decode("ascii", "replace"))
            except Exception:
                lines.append("Game ID: <unreadable>")
            if gs is not None:
                lines.append("Wins: " + ", ".join(f"{n}={w}" for n, w in zip(OPPONENTS, gs.wins)) + f" (total {sum(gs.wins)})")
                lines.append(f"Battles {gs.battles}, record wins {gs.total_wins}, losses {gs.losses}, least turns {gs.least_turns}, "
                             f"1v1 {gs.one_on_one}, tag {gs.tag_team}, royale {gs.battle_royale}")
                lines.append(f"Rank points {gs.rank_points}, BP {gs.money}, peak BP {self.peak_money}")
            done, total = len(self.ap.checked), len(self.ap.all_locations)
            lines.append(f"Checks done {done} / {total}; items received {len(self.ap.items)}, applied {self.applied}, "
                         f"additive_through {self.additive_through}, decks_written {self.decks_written}")
            lines.append(f"Boosts: {self.inj.boosts}; banked: {self.banked_boosts}")
            lines.append(f"Enemy decks: enabled={self.settings.enemy_decks_enabled}, params={self._enemy_params()}, "
                         f"baseline stored={self.enemy_baseline is not None}")
            for label, addr, size in (
                ("Rank points + BP", RANK_POINTS_ADDR, ECON_BLOCK_LEN),
                ("Stats block", STATS_BLOCK_ADDR, STATS_BLOCK_LEN),
                ("Deck 1", DECK1_ADDR, 36),
                ("Deck 2", DECK2_ADDR, 36),
                ("Cards", CARD_BASE, CARD_COUNT),
                ("Enemy deck Dan #1", enemy_decks.ENEMY_DECK_BASE, enemy_decks.DECK_STRIDE),
                ("Enemy deck Marduk #6", enemy_decks.ENEMY_DECK_BASE + enemy_decks.deck_offset(15, 5), enemy_decks.DECK_STRIDE),
            ):
                try:
                    lines.append(f"{label} @0x{addr:08X} [{size}]: {hex_str(self.mem.read(addr, size), size)}")
                except Exception as ex:
                    lines.append(f"{label} @0x{addr:08X}: unreadable ({ex})")
        except Exception as ex:                       # a report must never break the goal handling
            lines.append(f"(report incomplete: {ex})")
        lines.append("===== END GOAL REPORT =====")
        for line in lines:
            self.log(line)
        try:
            (app_dir() / "goal_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ items
    def _apply(self, idx: int, item_id: int) -> str:
        kind, arg = classify(item_id)
        if kind == "bakugan":
            self.inj.write_bakugan(arg)
            return "injected"
        if kind == "card":
            self.inj.write_card(arg)
            return "injected"
        if kind in ADDITIVE:
            if idx < self.additive_through:
                return "done"          # already added in an earlier run; never add twice
            (self.inj.add_rank_points if kind == "rank" else self.inj.add_money)(arg)
            self.additive_through = idx + 1
            self._save("additive_through", self.additive_through)
            return "injected"
        if kind in BOOSTS:
            if idx < self.additive_through:
                return "done"
            self._give_boost(str(idx), kind, arg)
            self.additive_through = idx + 1
            self._save("additive_through", self.additive_through)
            return "injected"
        if kind == "nerf":
            return "injected"          # nothing to apply now: the enemy deck maintenance counts nerf items itself
        return "unsupported"

    # ------------------------------------------------------------------ stat boosters
    def _boost_targets(self, kind: str):
        owned = sorted({arg for k, arg in (classify(it["item"]) for it in self.ap.items) if k == "bakugan"})
        return [c for c in owned if self.inj.boost_room(c, kind)]

    def _give_boost(self, seed: str, kind: str, amount: int, bank: bool = True) -> bool:
        """Put a booster on a random owned Bakugan that still has room. With none available it is banked
        and spent later (so a booster that arrives before any Bakugan is never lost)."""
        targets = self._boost_targets(kind)
        if not targets:
            if bank:
                self.banked_boosts[kind] = self.banked_boosts.get(kind, 0) + 1
                self._save("banked_boosts", self.banked_boosts)
                self.log("No Bakugan can take this booster yet; it will be applied when one can")
            return False
        combined = random.Random(f"{self.key}:{seed}").choice(targets)   # same pick every time for a seed
        self.inj.add_boost(combined, kind, amount)
        self._save("boosts", {str(c): v for c, v in self.inj.boosts.items()})
        what = "+30 G-Power" if kind == "gpboost" else "+1 to all stats"
        self.log(f"{what} -> {bakugan_name(combined)}")
        return True

    def _spend_banked_boosts(self):
        for kind in list(self.banked_boosts):
            while self.banked_boosts.get(kind, 0) > 0:
                n = self.banked_boosts[kind]
                if not self._give_boost(f"bank:{kind}:{n}:{len(self.inj.boosts)}", kind,
                                        30 if kind == "gpboost" else 1, bank=False):
                    break
                self.banked_boosts[kind] = n - 1
                self._save("banked_boosts", self.banked_boosts)

    def _process_items(self):
        with self.mem.batch("received items"):
            self._process_items_inner()

    def _process_items_inner(self):
        if not self.ap.connected or self.gs is None:
            return  # only write while the game's memory is readable
        items = self.ap.items
        while self.applied < len(items):
            idx = self.applied
            item = items[idx]
            name = self.ap.item_name(item["item"])
            try:
                status = self._apply(idx, item["item"])
            except (MemoryAccessError, InjectionError) as e:
                msg = f"Can't apply {name} yet: {e}"
                if msg != self._last_inject_err:
                    self.log(msg)
                    self._last_inject_err = msg
                self.item_status[idx] = "pending"
                return
            self._last_inject_err = None
            self.item_status[idx] = status
            self.applied += 1
            self._save("items_applied", self.applied)
            sender = self.ap.player_name(item.get("player"))
            if status == "injected":
                self.log(f"Received {name} from {sender}")
            elif status == "unsupported":
                self.log(f"Received {name} from {sender} (not supported by the client yet, nothing written)")

    # ------------------------------------------------------------------ enemy decks
    def _enemy_params(self):
        sd = self.ap.slot_data
        mode = int(sd.get("enemy_deck_randomization", 0) or 0)
        tier = int(sd.get("enemy_stats", 0) or 0)
        seed = int(sd.get("enemy_decks_seed", 0) or 0)
        nerfs = sum(1 for it in self.ap.items[:self.applied] if classify(it["item"])[0] == "nerf")
        return seed, mode, tier, nerfs

    def _enemy_block(self, params):
        if self._enemy_expected is None or self._enemy_expected[0] != params:
            seed, mode, tier, nerfs = params
            self._enemy_expected = (params, enemy_decks.build_enemy_block(self.enemy_baseline, seed, mode, tier, nerfs))
        return self._enemy_expected[1]

    def _maintain_enemy_decks(self, force: bool = False):
        """Keep the enemy decks in memory equal to the generated ones. The game loads them from the ROM, so after a
        reload they are simply written again. Runs every couple of seconds, once all received items are applied."""
        if not self.settings.enemy_decks_enabled:
            if (not self._enemy_off_logged and self.ap.connected
                    and enemy_decks.needs_work(*self._enemy_params()[1:])):
                self.log("Enemy deck changes are turned off in Settings (experimental), enemy decks left alone")
                self._enemy_off_logged = True
            return
        if not self.ap.connected or self.applied < len(self.ap.items):
            return
        seed, mode, tier, nerfs = params = self._enemy_params()
        if not enemy_decks.needs_work(mode, tier, nerfs):
            return
        if not force and time.time() - self._last_enemy_check < 2.0:
            return
        self._last_enemy_check = time.time()
        try:
            if not self._ensure_enemy_root():
                return
            current = self.mem.read(enemy_decks.ENEMY_DECK_BASE, enemy_decks.BLOCK_LEN)
            if self.enemy_baseline is None:
                problem = enemy_decks.structure_problem(current)
                if problem:
                    if problem != self._enemy_wait_logged:
                        self.log(f"Enemy decks not changed: memory doesn't look like the enemy decks yet ({problem}). "
                                 "Wrong game version, or the game hasn't loaded them")
                        self._enemy_wait_logged = problem
                    if self.settings.enemy_delta_override.strip() == "":
                        self.rebase.enemy_delta = None      # look somewhere else next time
                    return
                self.enemy_baseline = current
                self._save("enemy_baseline", current.hex())
            expected = self._enemy_block(params)
            if enemy_decks.matches(expected, current):
                return
            # Never write unless what is in memory right now still has the documented shape
            problem = enemy_decks.structure_problem(current)
            if problem:
                if problem != self._enemy_wait_logged:
                    self.log(f"Enemy decks not rewritten: memory changed shape ({problem})")
                    self._enemy_wait_logged = problem
                return
            writes = enemy_decks.diff_writes(expected, current)
            with self.mem.batch("enemy decks"):
                for offset, data in writes:           # only the Bakugan records / card ids that differ
                    self.mem.write(enemy_decks.ENEMY_DECK_BASE + offset, data)
            if not enemy_decks.matches(expected, self.mem.read(enemy_decks.ENEMY_DECK_BASE, enemy_decks.BLOCK_LEN)):
                raise InjectionError("Read-back mismatch writing the enemy decks")
            self.log(f"Wrote the enemy decks ({len(writes)} fields)" + (f", {nerfs} nerf(s) applied" if nerfs else ""))
        except (MemoryAccessError, InjectionError) as ex:
            msg = f"Couldn't write the enemy decks: {ex}"
            if msg != self._last_inject_err:
                self.log(msg)
                self._last_inject_err = msg

    # ------------------------------------------------------------------ starter decks
    def _maybe_write_decks(self, force: bool = False) -> bool:
        decks = self.ap.slot_data.get("starter_decks") or []
        if not decks or not self.ap.connected or self.gs is None:
            return False
        if self.decks_written and not force:
            return False
        if self.applied < len(self.ap.items):
            return False
        kinds = [classify(it["item"]) for it in self.ap.items]
        owned_bakugan = {arg for kind, arg in kinds if kind == "bakugan"}
        owned_cards = {arg for kind, arg in kinds if kind == "card"}
        for deck in decks[:2]:   # never put items we don't own into a deck: the game can crash
            if not (set(deck.get("bakugan", [])) <= owned_bakugan
                    and set(deck.get("gate_cards", [])) | set(deck.get("ability_cards", [])) <= owned_cards):
                if not self._decks_wait_logged:
                    self.log("Waiting for the starter deck items before writing the decks")
                    self._decks_wait_logged = True
                return False
        try:
            for slot, deck in enumerate(decks[:2]):
                self.inj.write_deck(slot, deck)
        except (MemoryAccessError, InjectionError) as e:
            self.log(f"Couldn't write the starter decks: {e}")
            return False
        self.decks_written = True
        self._save("decks_written", 1)
        self.log("Wrote the starter decks to decks 1 and 2")
        return True

    async def write_starter_decks(self):
        if not self._maybe_write_decks(force=True):
            self.log("Starter decks not written (connect, load a save and wait for all items first)")

    # ------------------------------------------------------------------ inventory tools (buttons)
    async def refresh_inventory(self):
        with self.mem.batch("refresh inventory"):
            await self._refresh_inventory()

    async def _refresh_inventory(self):
        """Wipe all Bakugan and cards, then write back everything received (BP/rank are never re-added)."""
        if not self.ap.connected or self.gs is None:
            self.log("Refresh Inventory needs Dolphin and Archipelago connected")
            return
        received = [classify(it["item"]) for it in self.ap.items]
        try:
            for kind, arg in received:        # validate first so a missing hex map can't leave you with nothing
                if kind == "bakugan":
                    self.inj._bakugan_target(arg)
            self.inj.wipe_inventory()
            bakugan = cards = 0
            for kind, arg in received:
                if kind == "bakugan":
                    self.inj.write_bakugan(arg)
                    bakugan += 1
                elif kind == "card":
                    self.inj.write_card(arg)
                    cards += 1
        except (MemoryAccessError, InjectionError) as e:
            self.log(f"Refresh Inventory failed: {e}")
            return
        self.log(f"Refresh Inventory: wiped, then wrote {bakugan} Bakugan and {cards} cards")
        self._maintain_enemy_decks(force=True)

    async def missing_inventory(self):
        with self.mem.batch("missing inventory"):
            await self._missing_inventory()

    async def _missing_inventory(self):
        """Write only the received Bakugan/cards that aren't in memory (desync recovery)."""
        if not self.ap.connected or self.gs is None:
            self.log("Missing Inventory needs Dolphin and Archipelago connected")
            return
        fixed = 0
        try:
            for it in self.ap.items:
                kind, arg = classify(it["item"])
                if kind == "bakugan" and not self.inj.bakugan_present(arg):
                    self.inj.write_bakugan(arg)
                    self.log(f"Restored {bakugan_name(arg)}")
                    fixed += 1
                elif kind == "card" and not self.inj.card_present(arg):
                    self.inj.write_card(arg)
                    fixed += 1
        except (MemoryAccessError, InjectionError) as e:
            self.log(f"Missing Inventory failed: {e}")
            return
        self.log(f"Missing Inventory: restored {fixed} item(s)")

    async def test_inject(self, kind: str, value: int):
        """Write one Bakugan (combined id) or card (index) straight into memory, no Archipelago needed.
        Used to check that addresses and hex-map stats work. Never recorded as a received item."""
        if not self.mem.is_hooked() and not self.mem.hook():
            self.log("Test Inject needs Dolphin running with the game loaded")
            return
        try:
            if kind == "bakugan":
                self.inj.write_bakugan(value)
                self.log(f"Test Inject: wrote {bakugan_name(value)}")
            elif kind == "card":
                if not 0 <= value < CARD_COUNT:
                    self.log(f"Test Inject: card index must be 0-{CARD_COUNT - 1}")
                    return
                self.inj.write_card(value)
                self.log(f"Test Inject: wrote card {value} ({'gate' if value < 98 else 'ability'})")
            else:
                self.log(f"Test Inject: unknown kind {kind!r}")
        except (MemoryAccessError, InjectionError) as ex:
            self.log(f"Test Inject failed: {ex}")

    # ------------------------------------------------------------------ memory tab helpers
    def read_memory(self, address: int, size: int) -> bytes:
        if not self.raw.is_hooked() and not self.raw.hook():
            raise MemoryAccessError("Dolphin not found")
        return self.raw.read(address, size)          # real Dolphin addresses (use the presets for documented ones)

    def write_memory(self, address: int, data: bytes) -> None:
        if not self.raw.is_hooked() and not self.raw.hook():
            raise MemoryAccessError("Dolphin not found")
        self.raw.write(address, data)
        self.log(f"Wrote {len(data)} byte(s) to 0x{address:08X}")

    # ------------------------------------------------------------------ snapshot for the GUI
    def publish(self):
        gs = self.gs
        wins = gs.wins if gs else [0] * len(OPPONENTS)
        cap, cap_source = self.cap()
        opponents = []
        for i, name in enumerate(OPPONENTS):
            eligible = min(wins[i], cap)
            done = sum(1 for n in range(1, eligible + 1) if defeat_location_id(i, n) in self.ap.checked)
            opponents.append({"name": name, "wins": wins[i], "eligible": eligible, "checked": done})
        elapsed = time.time() - self.started
        items = [
            {"index": i, "name": self.ap.item_name(it["item"]), "from": self.ap.player_name(it.get("player")),
             "status": self.item_status.get(i, "pending")}
            for i, it in enumerate(self.ap.items)
        ]
        self.events.put(("snapshot", {
            "dolphin": self.dolphin_status,
            "ap": self.ap.status,
            "player_name": self.player_name,
            "opponents": opponents,
            "total_wins": sum(wins),
            "session_wins": self.session_wins,
            "wins_per_hour": self.session_wins / (elapsed / 3600) if elapsed > 60 else 0.0,
            "session_seconds": int(elapsed),
            "battles": gs.battles if gs else 0,
            "losses": gs.losses if gs else 0,
            "rank_points": gs.rank_points if gs else None,
            "money": gs.money if gs else None,
            "checks_done": len(self.ap.checked),
            "checks_total": len(self.ap.all_locations),
            "cap": cap,
            "cap_source": cap_source,
            "goal_sent": self.goal_sent,
            "items": items,
            "items_applied": self.applied,
        }))
