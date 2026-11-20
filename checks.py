"""Decides which Archipelago locations the current game state has completed, and whether the goal is met."""
from typing import Dict, Set

from constants import (BATTLE_TYPE_IDS, BATTLE_TYPE_WINS, COLLECTOR_BAKUGAN_ID, COLLECTOR_CARD_ID,
                       ENEMY_TO_INDEX, LOWEST_TURN_BASE, MONEY_BAG_MILESTONES, RANK_1_POINTS, RANK_MILESTONES,
                       TOURNAMENTS, defeat_location_id)
from gamestate import GameState


def tournament_met(requirements: Dict[str, int], wins) -> bool:
    return all(wins[ENEMY_TO_INDEX[name]] >= needed for name, needed in requirements.items())


def achieved_locations(gs: GameState, defeat_cap: int, bakugan_target: int, card_target: int,
                       bakugan_owned: int, cards_owned: int, peak_money: int) -> Set[int]:
    """Every location id the game state currently satisfies. The caller keeps only the ones that
    actually exist (and are still missing) in the player's slot."""
    out: Set[int] = set()

    for i, count in enumerate(gs.wins):                       # Defeat <opponent> (Nx)
        for n in range(1, min(count, defeat_cap) + 1):
            out.add(defeat_location_id(i, n))

    for loc_id, _name, _prize, reqs in TOURNAMENTS:           # medals / crests from cumulative wins
        if tournament_met(reqs, gs.wins):
            out.add(loc_id)

    if gs.rank_points is not None:                            # brawler rank milestones
        out.update(loc for loc, points in RANK_MILESTONES if gs.rank_points >= points)

    if gs.least_turns > 0:                                    # "Win Battle in N Turns or Less"
        out.update(LOWEST_TURN_BASE + t for t in range(3, 9) if gs.least_turns <= t)

    for loc, wins in zip(BATTLE_TYPE_IDS, (gs.one_on_one, gs.tag_team, gs.battle_royale)):
        if wins >= BATTLE_TYPE_WINS:
            out.add(loc)

    out.update(loc for loc, bp in MONEY_BAG_MILESTONES if peak_money >= bp)   # money bags, by peak BP

    if bakugan_owned >= bakugan_target:
        out.add(COLLECTOR_BAKUGAN_ID)
    if cards_owned >= card_target:
        out.add(COLLECTOR_CARD_ID)
    return out


def goal_reached(gs: GameState, slot_data: dict) -> bool:
    mode = slot_data.get("goal", 0)
    if mode == 1:                                             # Rank 1 Brawler
        return gs.rank_points is not None and gs.rank_points >= RANK_1_POINTS
    index = 6 if mode == 0 else int(slot_data.get("target_tournament", 6))   # story = Final Battle
    return tournament_met(TOURNAMENTS[index][3], gs.wins)
