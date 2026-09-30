from collections.abc import Callable
from itertools import combinations

from fba.contracts.base import DataError
from fba.contracts.config import StarterSlot
from fba.core.roster import match_slots


def legal_assignment(
    slots: tuple[StarterSlot, ...], positions: dict[str, tuple[str, ...]], players: tuple[str, ...]
) -> dict[str, str] | None:
    eligible = tuple(
        tuple(bool(set(positions[p]).intersection(s.eligible_positions)) for s in slots)
        for p in players
    )
    chosen, assignment = match_slots(eligible, tuple(range(len(players))), len(slots))
    if len(chosen) != len(players):
        return None
    return {slots[s].id: players[p] for s, p in enumerate(assignment) if p is not None}


def best_lineup(
    slots: tuple[StarterSlot, ...],
    positions: dict[str, tuple[str, ...]],
    objective: Callable[[tuple[str, ...]], float],
    tolerance: float,
    required: tuple[str, ...] = (),
) -> tuple[dict[str, str], float]:
    ids = tuple(sorted(positions))
    if set(required) - set(ids):
        raise DataError("lineup.required: unavailable locked player")
    best: tuple[str, ...] | None = None
    best_value = float("-inf")
    best_assignment: dict[str, str] = {}
    for size in range(len(required), min(len(slots), len(ids)) + 1):
        for players in combinations(ids, size):
            if not set(required).issubset(players):
                continue
            assignment = legal_assignment(slots, positions, players)
            if assignment is None:
                continue
            value = objective(players)
            if value > best_value + tolerance or (
                abs(value - best_value) <= tolerance
                and (best is None or (-len(players), players) < (-len(best), best))
            ):
                best, best_value, best_assignment = players, value, assignment
    if best is None:
        raise DataError("lineup: no legal assignment for the locked players")
    return best_assignment, best_value
