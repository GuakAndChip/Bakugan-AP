"""Static game data copied from the apworld (Items.py / Locations.py)."""
from typing import Dict, List, Tuple

ATTRIBUTE_NAMES: List[str] = ["Pyrus", "Aquos", "Subterra", "Haos", "Darkus", "Ventus"]

SPECIES_ROSTER: List[Tuple[int, str, List[int]]] = [
    (0x00, "Dragonoid", [0]), (0x01, "Delta Dragonoid", [0]), (0x04, "Tigrerra", [3]),
    (0x05, "Blade Tigrerra", [3]), (0x06, "Preyas", [1]), (0x07, "Angelo", [1]),
    (0x08, "Diablo", [1]), (0x09, "Gorem", [2]), (0x0A, "Hammer Gorem", [2]),
    (0x0B, "Skyress", [5]), (0x0C, "Storm Skyress", [5]), (0x0D, "Hydranoid", [4]),
    (0x0E, "Dual Hydranoid", [4]), (0x19, "Reaper", [1, 2, 3, 4, 5]), (0x1A, "Cycloid", [2]),
    (0x1B, "Sirenoid", [1]), (0x1C, "Fourtress", [0]), (0x1D, "Tentaclear", [3]),
    (0x1E, "Harpus", [5]), (0x27, "Juggernoid", [0, 1, 2, 3, 4, 5]),
    (0x28, "Robotallion", [0, 1, 2, 3, 4, 5]), (0x29, "Saurus", [0, 1, 2, 3, 4, 5]),
    (0x2A, "Serpenoid", [0, 1, 2, 3, 4, 5]), (0x2B, "Falconeer", [0, 1, 2, 3, 4, 5]),
    (0x2C, "Stinglash", [0, 1, 2, 3, 4, 5]), (0x2D, "Griffon", [0, 1, 2, 3, 4, 5]),
    (0x2E, "Laser Man", [0, 1, 2, 3, 4, 5]), (0x2F, "Centipoid", [0, 1, 2, 3, 4, 5]),
    (0x30, "Fear Ripper", [0, 1, 2, 3, 4, 5]), (0x31, "Siege", [0, 1, 2, 3, 4, 5]),
    (0x32, "Terror Claw", [0, 1, 2, 3, 4]), (0x34, "Gargonoid", [0, 1, 2, 4, 5]),
    (0x3C, "Warius", [0]), (0x3D, "Monarus", [0, 3, 5]), (0x3E, "Leonidas", [0, 1, 2, 3, 4, 5]),
    (0x3F, "Omega Leonidas", [0, 1, 2, 3, 4, 5]), (0x40, "Vladitor", [4]),
    (0x41, "Battle Axe Vladitor", [4]),
]

# combined id = 6 * species + attribute  ->  display name
VALID_COMBINED: Dict[int, str] = {
    6 * sp + at: f"{ATTRIBUTE_NAMES[at]} {name}"
    for sp, name, attrs in SPECIES_ROSTER for at in attrs
}
