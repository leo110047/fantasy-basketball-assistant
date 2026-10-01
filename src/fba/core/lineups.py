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
    *,
    batch_objective: Callable[[tuple[tuple[str, ...], ...]], tuple[float, ...]] | None = None,
    batch_size: int = 1,
    subset_solver: Callable[
        [tuple[tuple[bool, ...], ...], int],
        tuple[tuple[tuple[int, ...], tuple[int | None, ...]], ...],
    ]
    | None = None,
    maximum: float | Callable[[float], float | None] | None = None,
) -> tuple[dict[str, str], float]:
    if batch_size < 1:
        raise DataError("lineup.batch_size: must be positive")
    ids = tuple(sorted(positions))
    if set(required) - set(ids):
        raise DataError("lineup.required: unavailable locked player")
    best: tuple[str, ...] | None = None
    best_value = float("-inf")
    best_assignment: dict[str, str] = {}
    eligibility = tuple(
        tuple(bool(set(positions[p]).intersection(s.eligible_positions)) for s in slots)
        for p in ids
    )
    candidates = (subset_solver or legal_subsets)(eligibility, len(slots))
    if required:
        required_indices = {ids.index(p) for p in required}
        candidates = tuple(row for row in candidates if required_indices.issubset(row[0]))
    if candidates and maximum is not None:
        chosen, assigned = min(candidates, key=lambda row: (-len(row[0]), row[0]))
        preferred = tuple(ids[p] for p in chosen)
        values = batch_objective((preferred,)) if batch_objective else (objective(preferred),)
        if len(values) != 1:
            raise DataError("lineup.objective: batch result length differs from candidates")
        value = values[0]
        ceiling = maximum(value) if callable(maximum) else maximum
        if ceiling is not None and value >= ceiling:
            return {slots[s].id: ids[p] for s, p in enumerate(assigned) if p is not None}, value
    # Bound the temporary draw tensor without dropping any legal candidate.
    for start in range(0, len(candidates), batch_size):
        chunk = tuple(
            (tuple(ids[p] for p in chosen), assigned)
            for chosen, assigned in candidates[start : start + batch_size]
        )
        rows = tuple(players for players, _ in chunk)
        values = batch_objective(rows) if batch_objective else tuple(objective(p) for p in rows)
        if len(values) != len(chunk):
            raise DataError("lineup.objective: batch result length differs from candidates")
        for (players, assigned), value in zip(chunk, values, strict=True):
            if value > best_value + tolerance or (
                abs(value - best_value) <= tolerance
                and (best is None or (-len(players), players) < (-len(best), best))
            ):
                best, best_value = players, value
                best_assignment = {
                    slots[s].id: ids[p] for s, p in enumerate(assigned) if p is not None
                }
    if best is None:
        raise DataError("lineup: no legal assignment for the locked players")
    return best_assignment, best_value


def legal_subsets(
    eligibility: tuple[tuple[bool, ...], ...], slots: int
) -> tuple[tuple[tuple[int, ...], tuple[int | None, ...]], ...]:
    """Enumerate exact matching by position signature, independent of player IDs."""
    result: list[tuple[tuple[int, ...], tuple[int | None, ...]]] = []
    for size in range(min(slots, len(eligibility)) + 1):
        for chosen in combinations(range(len(eligibility)), size):
            matched, assignment = match_slots(eligibility, chosen, slots)
            if len(matched) == size:
                result.append((chosen, assignment))
    return tuple(result)
