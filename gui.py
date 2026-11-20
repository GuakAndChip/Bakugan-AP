"""Tkinter front end. All real work happens in engine.py; this file only displays and forwards clicks."""
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config import Settings
from constants import APP_NAME, PLAYER_NAME_ADDR, WIN_ARRAY_ADDR
from engine import Engine
from hexmap import parse_hex_bytes
from bakugan_data import VALID_COMBINED
from memory import MemoryAccessError

MAX_LOG_LINES = 2000

# (label, Settings attribute, type) for the Settings tab
SETTING_FIELDS = [
    ("Poll interval (seconds)", "poll_interval", float),
    ("Bakugan hex map JSON", "hex_map_path", str),
]


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def hex_dump(address: int, data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02X}" for b in chunk).ljust(16 * 3 - 1)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{address + off:08X}  {hexes}  {text}")
    return "\n".join(lines)


class App:
    def __init__(self, root: tk.Tk, engine: Engine, settings: Settings):
        self.root, self.engine, self.settings = root, engine, settings
        root.title(APP_NAME)
        root.geometry("900x660")
        root.minsize(780, 540)
        self._last_opp = None
        self._last_items = None
        self._build_header()
        self._build_tabs()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll()

    # ------------------------------------------------------------------ layout
    def _build_header(self):
        box = ttk.LabelFrame(self.root, text="Archipelago connection", padding=8)
        box.pack(fill="x", padx=8, pady=(8, 4))
        self.server_var = tk.StringVar(value=self.settings.server)
        self.slot_var = tk.StringVar(value=self.settings.slot)
        self.pass_var = tk.StringVar(value=self.settings.password)
        for col, (label, var, width, show) in enumerate([
            ("Server", self.server_var, 26, ""),
            ("Slot", self.slot_var, 16, ""),
            ("Password", self.pass_var, 14, "*"),
        ]):
            ttk.Label(box, text=label).grid(row=0, column=col * 2, sticky="e")
            ttk.Entry(box, textvariable=var, width=width, show=show).grid(row=0, column=col * 2 + 1, padx=(4, 10))
        ttk.Button(box, text="Connect", command=self.on_connect).grid(row=0, column=6, padx=2)
        ttk.Button(box, text="Disconnect", command=self.on_disconnect).grid(row=0, column=7, padx=2)
        tools = ttk.Frame(box)
        tools.grid(row=1, column=0, columnspan=8, sticky="w", pady=(8, 0))
        ttk.Button(tools, text="Refresh Inventory", command=self.on_refresh_inventory).pack(side="left", padx=(0, 6))
        ttk.Button(tools, text="Missing Inventory", command=self.on_missing_inventory).pack(side="left", padx=6)
        ttk.Button(tools, text="Write Starter Decks", command=self.on_write_decks).pack(side="left", padx=6)
        ttk.Button(tools, text="Test Inject", command=self.on_test_inject).pack(side="left", padx=6)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10)
        self.dolphin_var = tk.StringVar(value="Dolphin: starting...")
        self.ap_var = tk.StringVar(value="Archipelago: disconnected")
        self.player_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.dolphin_var).pack(side="left", padx=(0, 18))
        ttk.Label(bar, textvariable=self.ap_var).pack(side="left", padx=(0, 18))
        ttk.Label(bar, textvariable=self.player_var).pack(side="left")

    def _build_tabs(self):
        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        for name, builder in [("Stats", self._build_stats), ("Log", self._build_log),
                              ("Items", self._build_items), ("Memory", self._build_memory),
                              ("Settings", self._build_settings)]:
            frame = ttk.Frame(tabs, padding=8)
            tabs.add(frame, text=name)
            builder(frame)

    def _build_stats(self, f):
        grid = ttk.Frame(f)
        grid.pack(fill="x")
        self.stat_vars = {}
        labels = ["Total wins", "Session wins", "Wins / hour", "Session time",
                  "Battles / losses", "Rank points", "BP", "Check cap",
                  "Checks sent", "Items received", "Items applied", "Goal"]
        for i, label in enumerate(labels):
            r, c = divmod(i, 4)
            cell = ttk.Frame(grid, padding=(0, 0, 24, 6))
            cell.grid(row=r, column=c, sticky="w")
            ttk.Label(cell, text=label, foreground="gray").pack(anchor="w")
            var = tk.StringVar(value="-")
            ttk.Label(cell, textvariable=var, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
            self.stat_vars[label] = var
        self.opp_tree = ttk.Treeview(f, columns=("opp", "wins", "checks"), show="headings", height=16)
        for col, text, width in [("opp", "Opponent", 160), ("wins", "Wins", 80), ("checks", "Checks sent", 140)]:
            self.opp_tree.heading(col, text=text)
            self.opp_tree.column(col, width=width, anchor="w")
        self.opp_tree.pack(fill="both", expand=True, pady=(8, 0))

    def _build_log(self, f):
        self.log_text = tk.Text(f, state="disabled", wrap="word", height=20)
        sb = ttk.Scrollbar(f, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    def _build_items(self, f):
        self.item_tree = ttk.Treeview(f, columns=("idx", "item", "from", "status"), show="headings")
        for col, text, width in [("idx", "#", 50), ("item", "Item", 260), ("from", "From", 160), ("status", "Status", 120)]:
            self.item_tree.heading(col, text=text)
            self.item_tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(f, command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.item_tree.pack(fill="both", expand=True)

    def _build_memory(self, f):
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.addr_var = tk.StringVar(value=f"{WIN_ARRAY_ADDR:08X}")
        self.len_var = tk.StringVar(value="16")
        self.bytes_var = tk.StringVar()
        ttk.Label(row, text="Address (hex)").pack(side="left")
        ttk.Entry(row, textvariable=self.addr_var, width=12).pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Length").pack(side="left")
        ttk.Entry(row, textvariable=self.len_var, width=6).pack(side="left", padx=(4, 12))
        ttk.Button(row, text="Read", command=self.on_mem_read).pack(side="left")
        ttk.Button(row, text="Win counts", command=lambda: self._preset(WIN_ARRAY_ADDR, 16)).pack(side="left", padx=(12, 2))
        ttk.Button(row, text="Player name", command=lambda: self._preset(PLAYER_NAME_ADDR, 16)).pack(side="left", padx=2)

        wrow = ttk.Frame(f)
        wrow.pack(fill="x", pady=8)
        ttk.Label(wrow, text="Bytes to write (hex)").pack(side="left")
        ttk.Entry(wrow, textvariable=self.bytes_var, width=50).pack(side="left", padx=(4, 8))
        ttk.Button(wrow, text="Write to address above", command=self.on_mem_write).pack(side="left")

        self.mem_out = tk.Text(f, height=18, font=("Courier", 10), state="disabled")
        self.mem_out.pack(fill="both", expand=True)

    def _build_settings(self, f):
        self.setting_vars = {}
        for r, (label, attr, _kind) in enumerate(SETTING_FIELDS):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(getattr(self.settings, attr)))
            ttk.Entry(f, textvariable=var, width=46).grid(row=r, column=1, sticky="w", padx=8)
            self.setting_vars[attr] = var
            if attr == "hex_map_path":
                ttk.Button(f, text="Browse...", command=self.on_browse_hexmap).grid(row=r, column=2)
        btns = ttk.Frame(f)
        btns.grid(row=len(SETTING_FIELDS), column=0, columnspan=3, sticky="w", pady=14)
        ttk.Button(btns, text="Save settings", command=self.on_save_settings).pack(side="left")

    # ------------------------------------------------------------------ actions
    def _apply_header(self):
        self.settings.server = self.server_var.get().strip()
        self.settings.slot = self.slot_var.get().strip()
        self.settings.password = self.pass_var.get()
        self.settings.save()

    def on_connect(self):
        if not self.slot_var.get().strip():
            messagebox.showwarning(APP_NAME, "Enter your slot name first.")
            return
        self._apply_header()
        self.engine.submit(self.engine.connect_ap())

    def on_disconnect(self):
        self.engine.submit(self.engine.disconnect_ap())

    def on_browse_hexmap(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.setting_vars["hex_map_path"].set(path)

    def on_save_settings(self):
        try:
            for _label, attr, kind in SETTING_FIELDS:
                text = self.setting_vars[attr].get().strip()
                setattr(self.settings, attr, int(text, 0) if kind is int else kind(text))
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"Invalid setting: {e}")
            return
        self.settings.save()
        self.engine.reload_hex_map()
        self.engine.log("Settings saved")

    def _confirm_and_run(self, message: str, coro_factory):
        if messagebox.askyesno(APP_NAME, message):
            self.engine.submit(coro_factory())

    def on_refresh_inventory(self):
        self._confirm_and_run("Wipe all Bakugan and cards from the game, then write back everything "
                              "you have received so far?", self.engine.refresh_inventory)

    def on_missing_inventory(self):
        self.engine.submit(self.engine.missing_inventory())

    def on_test_inject(self):
        """Small dialog: pick a Bakugan or a card and write it straight into the game (no Archipelago needed)."""
        win = tk.Toplevel(self.root)
        win.title("Test Inject")
        win.resizable(False, False)
        names = {f"{name} ({cid})": cid for cid, name in sorted(VALID_COMBINED.items(), key=lambda kv: kv[1])}
        ttk.Label(win, text="Bakugan").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        bak_var = tk.StringVar()
        ttk.Combobox(win, textvariable=bak_var, values=list(names), width=36).grid(row=0, column=1, padx=4)

        def inject_bakugan():
            cid = names.get(bak_var.get().strip())
            if cid is None:
                messagebox.showerror(APP_NAME, "Pick a Bakugan from the list.", parent=win)
                return
            self.engine.submit(self.engine.test_inject("bakugan", cid))

        ttk.Button(win, text="Inject Bakugan", command=inject_bakugan).grid(row=0, column=2, padx=8)

        ttk.Label(win, text="Card index (0-97 gate, 98-190 ability)").grid(row=1, column=0, padx=8, pady=8, sticky="e")
        card_var = tk.StringVar()
        ttk.Entry(win, textvariable=card_var, width=10).grid(row=1, column=1, padx=4, sticky="w")

        def inject_card():
            try:
                idx = int(card_var.get().strip())
            except ValueError:
                messagebox.showerror(APP_NAME, "Enter a card index from 0 to 190.", parent=win)
                return
            self.engine.submit(self.engine.test_inject("card", idx))

        ttk.Button(win, text="Inject Card", command=inject_card).grid(row=1, column=2, padx=8)
        ttk.Label(win, text="Writes directly to game memory. Results appear in the Log tab.").grid(
            row=2, column=0, columnspan=3, padx=8, pady=(0, 8))

    def on_write_decks(self):
        self._confirm_and_run("Overwrite decks 1 and 2 with this seed's starter decks?",
                              self.engine.write_starter_decks)

    def _preset(self, address: int, length: int):
        self.addr_var.set(f"{address:08X}")
        self.len_var.set(str(length))
        self.on_mem_read()

    def _show_mem(self, text: str):
        self.mem_out.configure(state="normal")
        self.mem_out.delete("1.0", "end")
        self.mem_out.insert("end", text)
        self.mem_out.configure(state="disabled")

    def on_mem_read(self):
        try:
            addr = int(self.addr_var.get().strip(), 16)
            size = int(self.len_var.get().strip(), 0)
            self._show_mem(hex_dump(addr, self.engine.read_memory(addr, size)))
        except (ValueError, MemoryAccessError) as e:
            self._show_mem(f"Error: {e}")

    def on_mem_write(self):
        try:
            addr = int(self.addr_var.get().strip(), 16)
            data = parse_hex_bytes(self.bytes_var.get())
            if not data:
                raise ValueError("no bytes to write")
            if not messagebox.askyesno(APP_NAME, f"Write {len(data)} byte(s) to 0x{addr:08X}?"):
                return
            self.engine.write_memory(addr, data)
            self._show_mem(hex_dump(addr, self.engine.read_memory(addr, len(data))))
        except (ValueError, MemoryAccessError) as e:
            self._show_mem(f"Error: {e}")

    def on_close(self):
        self._apply_header()
        self.engine.stop()
        self.root.destroy()

    # ------------------------------------------------------------------ live updates
    def _poll(self):
        latest = None
        try:
            while True:
                kind, payload = self.engine.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "snapshot":
                    latest = payload
        except queue.Empty:
            pass
        if latest is not None:
            self._render(latest)
        self.root.after(150, self._poll)

    def _append_log(self, line: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        extra = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log_text.delete("1.0", f"{extra + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _render(self, s: dict):
        self.dolphin_var.set(f"Dolphin: {s['dolphin']}")
        self.ap_var.set(f"Archipelago: {s['ap']}")
        self.player_var.set(f"Player: {s['player_name']}" if s["player_name"] else "")
        fmt = lambda v: "-" if v is None else f"{v:,}"
        values = {
            "Total wins": str(s["total_wins"]),
            "Session wins": str(s["session_wins"]),
            "Wins / hour": f"{s['wins_per_hour']:.1f}",
            "Session time": fmt_duration(s["session_seconds"]),
            "Battles / losses": f"{s['battles']} / {s['losses']}",
            "Rank points": fmt(s["rank_points"]),
            "BP": fmt(s["money"]),
            "Check cap": f"{s['cap']} per opponent ({s['cap_source']})",
            "Checks sent": f"{s['checks_done']} / {s['checks_total']}" if s["checks_total"] else "-",
            "Items received": str(len(s["items"])),
            "Items applied": str(s["items_applied"]),
            "Goal": "complete" if s["goal_sent"] else "in progress",
        }
        for k, v in values.items():
            self.stat_vars[k].set(v)

        opp_rows = [(o["name"], o["wins"], f"{o['checked']} / {o['eligible']}") for o in s["opponents"]]
        if opp_rows != self._last_opp:
            self._last_opp = opp_rows
            self.opp_tree.delete(*self.opp_tree.get_children())
            for row in opp_rows:
                self.opp_tree.insert("", "end", values=row)

        item_rows = [(i["index"] + 1, i["name"], i["from"], i["status"]) for i in s["items"]]
        if item_rows != self._last_items:
            self._last_items = item_rows
            self.item_tree.delete(*self.item_tree.get_children())
            for row in item_rows:
                self.item_tree.insert("", "end", values=row)


def run():
    settings = Settings.load()
    engine = Engine(settings)
    engine.start()
    root = tk.Tk()
    App(root, engine, settings)
    root.mainloop()
