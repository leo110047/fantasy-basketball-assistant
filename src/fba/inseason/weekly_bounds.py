"""Opponent-dependent bounds retaining numerator / denominator correlation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from fba.contracts.config import Linear, Term
from fba.formulas.categories import category_values
from fba.formulas.lineup import (
    comparison_thresholds,
    linear_roundoff,
    threshold_error,
    threshold_margin,
    threshold_votes,
)
from fba.formulas.simulation import comparison_margin, subset_bound
from fba.formulas.vector import category_points, week_outcome
from fba.inseason.lineup_bounds import term_interval

if TYPE_CHECKING:
    from fba.inseason.matchup import Array, Simulation


@dataclass
class ThresholdBounds:
    sim: Simulation
    thresholds: tuple[Array, ...]
    error: Array
    zero_votes: Array
    calibrated: dict[float, float] = field(default_factory=dict)

    @classmethod
    def create(
        cls, sim: Simulation, actual: Array, opponent: Array, draws: tuple[Array, ...]
    ) -> ThresholdBounds | None:
        if any(np.any(a < 0) or not np.isfinite(a).all() for a in (actual, *draws)) or any(
            not isinstance(c.formula, Linear)
            and (
                c.formula.zero_denominator != "zero"
                or any(t.coefficient < 0 for t in c.formula.denominator)
            )
            for c in sim.league.categories
        ):
            return None
        upper = actual.copy()
        for draw in draws:
            sim.check_limits()
            upper = subset_bound(
                {
                    "base": upper,
                    "draw": draw,
                    "fixed": np.asarray(1.0),
                    "direction": np.asarray(1.0),
                }
            )
        away = category_values(opponent, sim.league.categories, sim.axes)
        if not np.isfinite(upper).all() or not np.isfinite(away).all():
            return None
        thresholds: list[Array] = []
        errors: list[Array] = []
        zeros: list[Array] = []
        for i, category in enumerate(sim.league.categories):
            sim.check_limits()
            formula = category.formula
            if not np.isfinite(np.round(away[..., i], category.comparison_decimals)).all():
                return None
            threshold = comparison_thresholds(
                {"away": away[..., i], "decimals": np.asarray(category.comparison_decimals)}
            )
            numerator = formula.terms if isinstance(formula, Linear) else formula.numerator
            nerror = roundoff(sim, upper, numerator, len(draws) + 1)
            derror = np.zeros_like(nerror)
            zero = np.full_like(nerror, -1.0)
            if not isinstance(formula, Linear):
                derror = roundoff(sim, upper, formula.denominator, len(draws) + 1)
                zero = np.sign(
                    comparison_margin(
                        {
                            "home": np.zeros_like(nerror),
                            "away": away[..., i],
                            "decimals": np.asarray(category.comparison_decimals),
                        }
                    )
                )
            thresholds.append(threshold)
            errors.append(
                threshold_error(
                    {"numerator": nerror, "denominator": derror, "threshold": threshold}
                )
            )
            zeros.append(zero)
        error = np.stack(errors, axis=1)
        if not np.isfinite(error).all() or any(not np.isfinite(t).all() for t in thresholds):
            return None
        return cls(sim, tuple(thresholds), error, np.stack(zeros, axis=1))

    def contribution(self, draw: Array, *, origin: bool = False) -> Array:
        result: list[Array] = []
        for category, threshold in zip(self.sim.league.categories, self.thresholds, strict=True):
            formula = category.formula
            low, high = term_interval(
                draw,
                draw,
                formula.terms if isinstance(formula, Linear) else formula.numerator,
                self.sim.axes,
            )
            numerator = high if category.direction == "higher" else -low
            if isinstance(formula, Linear):
                dlow = dhigh = np.full_like(numerator, float(origin))
            else:
                dlow, dhigh = term_interval(draw, draw, formula.denominator, self.sim.axes)
            result.append(
                threshold_margin(
                    {"upper": numerator, "dlower": dlow, "dupper": dhigh, "threshold": threshold}
                )
            )
        result_array = np.stack(result, axis=-2)
        return (
            np.asfortranarray(result_array)
            if self.sim.league.scoring == "h2h_one_win"
            else result_array
        )

    def ceiling(self, upper: Array) -> float:
        votes = threshold_votes(
            {"upper": upper, "error": self.error, "zero_votes": self.zero_votes}
        )
        if self.sim.league.scoring == "h2h_one_win":
            raw = float(week_outcome(votes.sum(axis=-1), np.asarray(0.0)).mean())
            if raw not in self.calibrated:
                self.calibrated[raw] = self.sim.calibrated_score(raw).result
            return self.calibrated[raw]
        ties = np.array(
            [
                c.tie_value if self.sim.league.category_ties == "use_tie_value" else 0.0
                for c in self.sim.league.categories
            ]
        )
        scores = category_points({"differences": votes, "ties": ties}).sum(axis=-1)
        return self.sim.calibrated_score(float(scores.mean())).result


def roundoff(sim: Simulation, upper: Array, terms: tuple[Term, ...], additions: int) -> Array:
    return linear_roundoff(
        {
            "upper": upper,
            "additions": np.asarray(additions),
            "indices": np.asarray([sim.axes.index(t.stat_id) for t in terms], dtype=np.float64),
            "weights": np.asarray([t.coefficient for t in terms]),
        }
    )
