"""Fixed facts from the master reference doc and the apworld (Items.py / Locations.py / __init__.py)."""

APP_NAME = "Bakugan Archipelago Client"
GAME_NAME = "Bakugan Battle Brawlers"

# --- Memory addresses (Wii virtual addresses: MEM1 0x80000000, MEM2 0x90000000) ---
ROOT_BAKUGAN_ANCHOR = 0x9046FE77  # Bakugan inventory root; record at anchor + offset, 14 bytes each
CARD_BASE = 0x9047255C            # per card_hex_map.json (index 0 = 0x9047255C); 191 bytes, 0x00 unowned / 0x01 owned (0-97 gate, 98-190 ability)
CARD_COUNT = 191
DECK1_ADDR = 0x9047269C           # 36-byte active deck structures (verified in-game; stride 0x4C)
DECK2_ADDR = 0x904726E8
RANK_POINTS_ADDR = 0x90472840     # 4-byte big-endian
BP_ADDR = 0x90472844              # 4-byte big-endian, max 16,777,215
ECON_BLOCK_LEN = 8                # rank points + BP read in one go
BP_MAX = 16_777_215
RANK_MAX = 0xFFFFFFFF
STATS_BLOCK_ADDR = 0x9047284A     # 18-byte record stats + 2 padding + 16 enemy win bytes
STATS_BLOCK_LEN = 36
WIN_ARRAY_ADDR = 0x9047285E       # == STATS_BLOCK_ADDR + 20
PLAYER_NAME_ADDR = 0x80AC9E35     # MEM1, best-effort display only

OPPONENTS = [
    "Dan", "Runo", "Marucho", "Shun", "Julie", "Masquerade", "Billy", "Klaus",
    "Chan Lee", "Julio", "Komba", "Shuji", "Akira", "Jewls", "Jenny", "Marduk",
]
ENEMY_TO_INDEX = {name: i for i, name in enumerate(OPPONENTS)}

# --- Item ids ---
BAKUGAN_ITEM_BASE = 8800001       # + (6 * species_idx + attribute_idx)
BAKUGAN_COMBINED_COUNT = 6 * 0x41 + 6
BAKUGAN_RECORD_STRIDE = 20        # fallback offset = combined id * 20 when the hex map gives none
BAKUGAN_RECORD_LEN = 14
CARD_ITEM_BASE = 8801000          # + card index (0-190)
RANK_POINT_ITEMS = {8802001: 15000, 8802002: 30000}
GPOWER_ITEM = 8802003             # +30 G-Power on one Bakugan
STAT_BOOST_ITEM = 8802004         # +1 to every movement stat on one Bakugan
GPOWER_BOOST = 30
ENEMY_NERF_ITEM = 8802005
GPOWER_MAX = 999
STAT_UNIT = 10                    # movement stats are stored as 10x values (3.0 -> 0x1E)
STAT_MAX = 50                     # cap of 5
BP_ITEMS = {8803001: 500, 8803002: 1000, 8803003: 2500, 8803004: 5000, 8803005: 10000}

# --- Location ids ---
ENEMY_DEFEAT_BASE = 8810001       # defeat #n of opponent i = base + (n-1)*100 + i
MAX_DEFEATS = 10

TOURNAMENTS = [  # (location id, name, prize, {opponent: cumulative wins required})
    (8830001, "Neo Challengers Tournament", "Gold Medal", {"Shuji": 1, "Runo": 1, "Marucho": 1}),
    (8830002, "Supreme Tag Team Tournament", "Aquas Crest", {
        "Jenny": 1, "Jewls": 1, "Billy": 1, "Komba": 1, "Chan Lee": 1, "Julio": 1, "Runo": 2, "Marucho": 2}),
    (8830003, "Brave Brawlers Tournament", "Subterra Crest", {
        "Billy": 2, "Shuji": 2, "Akira": 1, "Julio": 2, "Chan Lee": 2, "Klaus": 1,
        "Julie": 1, "Runo": 3, "Marucho": 3, "Marduk": 1}),
    (8830004, "Maximum Power Tournament", "Ventus Crest", {
        "Jenny": 2, "Jewls": 2, "Chan Lee": 3, "Julie": 2, "Shun": 1, "Masquerade": 1}),
    (8830005, "Bakugan Master Cup", "Pyrus Crest", {"Komba": 2, "Chan Lee": 3, "Klaus": 2, "Dan": 1}),
    (8830006, "Ultimate Battle Tournament", "Haos Crest", {
        "Marucho": 4, "Julie": 3, "Runo": 4, "Shun": 2, "Dan": 2, "Masquerade": 2, "Marduk": 2}),
    (8830007, "Final Battle", "Darkus Crest", {"Marduk": 3}),
]

RANK_MILESTONES = [  # (location id, rank points required)
    (8840001, 146500), (8840002, 147500), (8840003, 148500), (8840004, 150000), (8840005, 160000),
    (8840006, 170000), (8840007, 230000), (8840008, 250000), (8840009, 300000), (8840010, 400000),
]
RANK_1_POINTS = 400000

LOWEST_TURN_BASE = 8850000        # + turns (3..8): least-turns record <= turns
BATTLE_TYPE_IDS = (8850021, 8850022, 8850023)  # 1-on-1, tag team, battle royale (10 wins each)
BATTLE_TYPE_WINS = 10
MONEY_BAG_MILESTONES = [(8850101, 5000), (8850102, 15000), (8850103, 30000), (8850104, 50000)]
COLLECTOR_BAKUGAN_ID = 8850201
COLLECTOR_CARD_ID = 8850202


def defeat_location_id(opp_index: int, n: int) -> int:
    return ENEMY_DEFEAT_BASE + (n - 1) * 100 + opp_index


def defeat_location_name(opponent: str, n: int) -> str:
    return f"Defeat {opponent}" if n == 1 else f"Defeat {opponent} ({n}x)"


# --- Archipelago protocol ---
AP_VERSION = (0, 6, 0)
ITEMS_HANDLING_ALL = 0b111
CLIENT_GOAL = 30
