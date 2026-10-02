"""Complete binary search over player appearances with legal daily domains."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING

import numpy as np

from fba.formulas.lineup import accumulation_error, threshold_accumulate
from fba.formulas.simulation import subset_bound
from fba.inseason.weekly_bounds import ThresholdBounds

if TYPE_CHECKING:
    from fba.inseason.matchup import Array
    from fba.inseason.weekly_lineups import Assignment, WeeklySearch


@dataclass
class PlayerSearch:
    search: WeeklySearch
    bounds: ThresholdBounds
    items: tuple[tuple[int, int, Array], ...]
    suffix: tuple[Array, ...]
    origin: Array

    @classmethod
    def create(cls, search: WeeklySearch) -> PlayerSearch | None:
        players = [
            (i, pid, day.draws[pid])
            for i, day in enumerate(search.days)
            for pid in sorted({p for row in day.rows for p in row.values()})
        ]
        # This only orders decisions; every legal include / exclude branch is
        # retained. Canonical daily row order still owns all score tie breaks.
        players.sort(key=lambda row: -float(np.sum(row[2])))
        bounds = ThresholdBounds.create(
            search.sim, search.actual, search.opponent, tuple(a for _, _, a in players)
        )
        if bounds is None:
            return None
        if search.sim.league.scoring == "h2h_one_win":
            bounds.error = np.asfortranarray(bounds.error)
            bounds.zero_votes = np.asfortranarray(bounds.zero_votes)
        origin = bounds.contribution(search.actual, origin=True)
        magnitude = np.abs(origin)
        items: list[tuple[int, int, Array]] = []
        for i, pid, draw in players:
            search.sim.check_limits()
            mask = sum(1 << j for j, row in enumerate(search.days[i].rows) if pid in row.values())
            margin = bounds.contribution(draw)
            magnitude = add_bound(magnitude, np.abs(margin))
            items.append((i, mask, margin))
        if not np.isfinite(magnitude).all():
            return None
        allowance = accumulation_error(
            {"magnitude": magnitude, "additions": np.asarray(len(items) + 2)}
        )
        bounds.error = add_bound(bounds.error, allowance)
        if not np.isfinite(bounds.error).all():
            return None
        suffix = [np.zeros_like(bounds.error)]
        for _, _, margin in reversed(items):
            suffix.append(add_bound(suffix[-1], margin, fixed=False))
        if any(not np.isfinite(a).all() for a in suffix):
            return None
        return cls(search, bounds, tuple(items), tuple(reversed(suffix)), origin)

    def run(self) -> None:
        self.seed_removals()
        domains = tuple((1 << len(day.rows)) - 1 for day in self.search.days)
        self.visit(0, self.origin, domains)

    def seed_removals(self) -> None:
        """Improve the incumbent only; the full legal domains remain intact."""
        lookup = tuple(
            {frozenset(row.values()): row for row in day.rows} for day in self.search.days
        )
        previous: tuple[Assignment, ...] = ()
        while previous != self.search.best:
            self.search.sim.check_limits()
            previous = self.search.best
            candidates = (
                (*previous[:i], alternative, *previous[i + 1 :])
                for i, row in enumerate(previous)
                for pid in row.values()
                if (alternative := lookup[i].get(frozenset(row.values()) - {pid})) is not None
            )
            while batch := tuple(islice(candidates, self.search.sim.params.lineup_batch.value)):
                self.search.consider((), np.zeros_like(self.search.actual), batch)

    def preferred(self, domains: tuple[int, ...]) -> tuple[Assignment, ...]:
        return tuple(
            day.rows[(mask & -mask).bit_length() - 1]
            for day, mask in zip(self.search.days, domains, strict=True)
        )

    def visit(self, index: int, upper: Array, domains: tuple[int, ...]) -> None:
        self.search.sim.check_limits()
        possible = self.preferred(domains)
        ceiling = self.bounds.ceiling(
            threshold_accumulate({"base": upper, "draw": self.suffix[index]})
        )
        if self.search.pruned(ceiling, possible):
            return
        if index == len(self.items):
            self.search.consider((), np.zeros_like(self.search.actual), (self.preferred(domains),))
            return
        day, mask, contribution = self.items[index]
        included, excluded = domains[day] & mask, domains[day] & ~mask
        if included:
            self.visit(
                index + 1,
                threshold_accumulate({"base": upper, "draw": contribution}),
                (*domains[:day], included, *domains[day + 1 :]),
            )
        if excluded:
            self.visit(index + 1, upper, (*domains[:day], excluded, *domains[day + 1 :]))


def add_bound(base: Array, draw: Array, *, fixed: bool = True) -> Array:
    return subset_bound(
        {
            "base": base,
            "draw": draw,
            "fixed": np.asarray(float(fixed)),
            "direction": np.asarray(1.0),
        }
    )
