"""Apply dated roster changes without overwriting earlier IL activations."""

from datetime import date
from typing import NamedTuple

from fba.contracts.base import DataError
from fba.contracts.inseason import InjuryReturn


class InvalidRosterChange(DataError):
    """An explicit move or conditional release no longer has a legal player."""


class RosterChange(NamedTuple):
    effective_on: date
    add: str
    drop: str | None


def apply_change(roster: tuple[str, ...], change: RosterChange) -> tuple[str, ...]:
    if len(set(roster)) != len(roster):
        raise InvalidRosterChange("roster_timeline: duplicate player in the initial roster")
    if change.add in roster or (change.drop is not None and change.drop not in roster):
        raise InvalidRosterChange(
            f"roster_timeline.{change.effective_on}: cannot add {change.add} / drop {change.drop}"
        )
    return tuple(sorted((*tuple(p for p in roster if p != change.drop), change.add)))


def merge_changes(
    moves: tuple[RosterChange, ...], returns: tuple[InjuryReturn, ...]
) -> tuple[RosterChange, ...]:
    # Same-day IL activation precedes the explicitly scheduled add/drop. This
    # permits that move to release a just-activated player, but never the reverse.
    events = [
        (r.effective_on, 0, i, RosterChange(r.effective_on, r.player_id, r.drop))
        for i, r in enumerate(returns)
    ]
    events.extend((m.effective_on, 1, i, m) for i, m in enumerate(moves))
    return tuple(row[3] for row in sorted(events))


def roster_after(
    roster: tuple[str, ...], changes: tuple[RosterChange, ...], on: date
) -> tuple[str, ...]:
    for change in changes:
        if change.effective_on <= on:
            roster = apply_change(roster, change)
    return tuple(sorted(roster))
