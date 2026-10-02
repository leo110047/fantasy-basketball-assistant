"""Complete search over exchangeable game counts, with exact daily feasibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from fba.formulas.lineup import accumulation_error, threshold_accumulate
from fba.formulas.simulation import mean_array
from fba.inseason.count_feasibility import CountFeasibility
from fba.inseason.weekly_bounds import ThresholdBounds
from fba.inseason.weekly_lineups import lineup_key
from fba.inseason.weekly_players import add_bound

if TYPE_CHECKING:
    from fba.inseason.matchup import Array
    from fba.inseason.weekly_lineups import Assignment, WeeklySearch
    from fba.inseason.weekly_samples import CountSamples


@dataclass
class CountSearch:
    search: WeeklySearch
    samples: CountSamples
    legal: CountFeasibility
    order: tuple[int, ...]
    bounds: ThresholdBounds | None
    origin: Array
    margins: tuple[tuple[Array, ...], ...]
    suffix: tuple[Array, ...]

    @classmethod
    def create(cls, search: WeeklySearch, samples: CountSamples) -> CountSearch:
        legal = CountFeasibility.create(search, samples)
        # Priority affects traversal only. Samples.total owns accumulation order.
        order = tuple(
            sorted(range(len(samples.choices)), key=lambda i: -float(samples.choices[i][-1].sum()))
        )
        bounds = ThresholdBounds.create(
            search.sim,
            search.actual,
            search.opponent,
            # Every candidate adds one already-rounded prefix per group. The
            # largest prefix bounds all its nonnegative choices; only group
            # additions contribute further total-rounding error.
            tuple(choices[-1] for choices in samples.choices),
        )
        origin = np.zeros_like(search.actual)
        margins: tuple[tuple[Array, ...], ...] = ()
        suffix: tuple[Array, ...] = ()
        if bounds is not None:
            origin = bounds.contribution(search.actual, origin=True)
            margins = grouped_margins(search, samples, order, bounds)
            magnitude = abs(origin)
            tails = [np.zeros_like(origin)]
            for choices in reversed(margins):
                search.sim.check_limits()
                magnitude = add_bound(magnitude, np.max(np.abs(np.stack(choices)), axis=0))
                tails.append(add_bound(tails[-1], np.max(np.stack(choices), axis=0)))
            allowance = accumulation_error(
                {"magnitude": magnitude, "additions": np.asarray(len(order) + 2)}
            )
            bounds.error = add_bound(bounds.error, allowance)
            suffix = tuple(reversed(tails))
            if any(not np.isfinite(a).all() for a in (*suffix, bounds.error)):
                bounds = None  # Enumerate counts without numeric pruning.
        return cls(search, samples, legal, order, bounds, origin, margins, suffix)

    def run(self) -> tuple[Array, tuple[Assignment, ...]]:
        self.consider(self.samples.counts(self.search.preferred))
        self.seed_removals()
        self.visit(0, self.origin, ())
        self.search.sim.check_limits()
        return self.search.best_total, self.search.best

    def seed_removals(self) -> None:
        previous: tuple[Assignment, ...] | None = None
        while previous != self.search.best:
            previous = self.search.best
            counts = self.samples.counts(previous)
            for i, value in enumerate(counts):
                if value > self.legal.required[0, i]:
                    self.consider((*counts[:i], value - 1, *counts[i + 1 :]))

    def consider(self, counts: tuple[int, ...]) -> None:
        sim = self.search.sim
        sim.check_limits()
        # Check legality before scoring: invalid count vectors must not introduce
        # formula-domain errors absent from every legal lineup.
        if not self.legal.feasible(np.asarray(counts, dtype=np.int64), 0):
            return
        total = self.samples.total(self.search.actual, counts)
        value = sim.calibrated_score(float(mean_array(sim.score(total, self.search.opponent)[1])))
        if value.result < self.search.best_value - sim.params.tolerance.value:
            return
        rows = self.legal.canonical(counts)
        assert rows is not None
        key = lineup_key(rows)
        if (
            value.result > self.search.best_value + sim.params.tolerance.value
            or self.search.best_key is None
            or key < self.search.best_key
        ):
            self.search.best = rows
            self.search.best_total = total
            self.search.best_value = value.result
            self.search.best_key = key

    def canonical_counts(self, chosen: tuple[int, ...]) -> tuple[int, ...]:
        counts = [0] * len(self.order)
        for i, value in zip(self.order, chosen, strict=True):
            counts[i] = value
        return tuple(counts)

    def pruned(self, index: int, prefix: Array, chosen: tuple[int, ...]) -> bool:
        if self.bounds is None:
            return False
        upper = self.bounds.ceiling(
            threshold_accumulate({"base": prefix, "draw": self.suffix[index]})
        )
        tolerance = self.search.sim.params.tolerance.value
        if upper < self.search.best_value - tolerance:
            return True
        if upper <= self.search.best_value + tolerance and self.search.best_key is not None:
            if lineup_key(self.search.preferred) >= self.search.best_key:
                return True  # The incumbent already owns the global canonical tie.
            maximum = (*chosen, *(int(self.legal.remaining[0, i]) for i in self.order[index:]))
            count = sum(maximum)
            if -count > self.search.best_key[0]:
                return True
            if -count == self.search.best_key[0]:
                possible = self.legal.canonical(self.canonical_counts(maximum))
                return possible is None or lineup_key(possible) >= self.search.best_key
        return False

    def visit(self, index: int, prefix: Array, chosen: tuple[int, ...]) -> None:
        self.search.sim.check_limits()
        if (
            sum(chosen) + sum(int(self.legal.required[0, i]) for i in self.order[index:])
            > self.legal.capacities[0]
        ):
            return
        if self.legal.cuts and not self.legal.prefix_possible(self.order, chosen):
            return
        if self.pruned(index, prefix, chosen):
            return
        if index == len(self.order):
            self.consider(self.canonical_counts(chosen))
            return
        group = self.order[index]
        for count in range(
            int(self.legal.remaining[0, group]), int(self.legal.required[0, group]) - 1, -1
        ):
            next_prefix = (
                threshold_accumulate({"base": prefix, "draw": self.margins[index][count]})
                if self.bounds is not None
                else prefix
            )
            self.visit(index + 1, next_prefix, (*chosen, count))


def grouped_margins(
    search: WeeklySearch, samples: CountSamples, order: tuple[int, ...], bounds: ThresholdBounds
) -> tuple[tuple[Array, ...], ...]:
    draws = tuple(a for i in order for a in samples.choices[i])
    flat: list[Array] = []
    width = search.sim.params.lineup_batch.value
    for start in range(0, len(draws), width):
        search.sim.check_limits()
        batch = bounds.contribution(np.stack(draws[start : start + width]))
        flat.extend(np.asfortranarray(a) for a in batch)
    result: list[tuple[Array, ...]] = []
    start = 0
    for i in order:
        end = start + len(samples.choices[i])
        result.append(tuple(flat[start:end]))
        start = end
    return tuple(result)
