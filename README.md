# Bakugan Archipelago Client (BakuganAP)

Standalone Windows client for the Bakugan Battle Brawlers apworld. Hooks Dolphin with pure ctypes, tracks stats, sends location checks and the goal, and writes received items into the game.

## Run from source

```
pip install -r requirements.txt
python main.py
```

The hex maps ship in the `data/` folder (`bakugan_hex_map_fixed.json`, `card_hex_map.json`); the client finds them there automatically. Start Dolphin with the game, enter server, slot and password, press Connect.

## Build the exe (players need nothing installed)

```
pip install pyinstaller
```
or just double-click `build.bat` (Windows, needs Python 3.10+ installed once, on the machine that builds). It produces `dist\BakuganAP.exe`, a single file players can double-click with no console window and nothing else to install. If the exe ever crashes at startup, the error is shown in a message box and saved to `~/.bakugan_ap_client/crash.log`.

or by hand:

```
pyinstaller --onefile --windowed --name BakuganAP --add-data "data;data" main.py
```

## What it does

- **Checks** (only locations that exist in your slot are sent): `Defeat <Name> (Nx)` up to the slot's `enemy_defeats`, tournament medals/crests (cumulative win thresholds), rank milestones, lowest-turn, battle-type wins, money bags (by peak BP) and the two collector milestones (targets come from slot data: `collector_bakugan_target` and `collector_card_target`, a percentage of what is in the seed; older seeds fall back to `collector_threshold`).
- **Goal**: story = Marduk 3 wins (Final Battle), tournament = its thresholds, rank 1 = 400,000 rank points. Reported once.
- **Items**: Bakugan (14-byte record from the hex map), cards (0x01 write), BP and rank points (added once, never again after a reconnect). Stat boosters (+30 G-Power, cap 999; +1 to all five movement stats, cap 5 each) go to a random owned Bakugan that still has room, are remembered per seed so Refresh/Missing Inventory keep them, and are held back until you own a Bakugan if none is available. 
- **Enemy decks** (EXPERIMENTAL, off by default: tick "Change enemy decks" in the Settings tab to enable; `enemy_deck_randomization`, `enemy_stats`, Enemy Difficulty Nerf items): the original 16 opponents x 6 decks are read once per seed (when the game has loaded them) and kept in the state file. Nothing is written unless all 96 decks match the documented layout exactly, and only the Bakugan records and card ids that differ are written (never the surrounding bytes). The decks that should be in memory are generated from `enemy_decks_seed` and rewritten whenever memory differs from them (the game reloads them from the ROM), and after Refresh Inventory. Decks 1-2 of each opponent are easy, 3-4 medium, 5-6 hard. Random decks keep each slot's attribute, use levels 1-4 / 4-7 / 7-10 by deck difficulty, G-Power from the tier range scaled by level, and movement stats weighted up with level. With randomization off, `enemy_stats` rescales the original G-Power and shifts movement stats. The 4 nerf items together remove 50% of G-Power and 5 movement points per enemy Bakugan.
- **Starter decks**: written to decks 1 and 2 after all items are in memory.
- **Buttons**: Refresh Inventory (wipe, then rewrite everything received), Missing Inventory (restore only what is gone), Write Starter Decks.
- **Test Inject**: pick one Bakugan or card and write it straight into the game, no Archipelago needed (for checking addresses and stats).
- **Debug log**: every memory write prints `[debug] WRITE 0xADDR [n] ok before=.. after=..` (after is read back; `MISMATCH` means it didn't stick). Bulk operations print the first 40 writes plus a count. When the goal is reached a full `GOAL REPORT` (game state, what the client did, raw rank/BP, stats, decks, cards, two enemy decks) is logged and saved to `goal_report.txt`. Everything in the Log tab is also appended to `client.log` in `~/.bakugan_ap_client/` (the tab itself only holds the latest 2000 lines). Turn the write logging off in Settings.
- **Memory tab**: raw read/write at any address.

Not in this version: shop lockout sweep.

Settings, progress state and the data-package cache live in `~/.bakugan_ap_client/`.
