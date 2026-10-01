"""Conservative score ceilings for choosing a subset of today's draws."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np

from fba.contracts.config import Linear, Term
from fba.formulas.categories import category_values
from fba.formulas.simulation import comparison_margin, linear_interval, mean_array, ratio_interval
from fba.formulas.vector import category_points, week_points

if TYPE_CHECKING:
    from fba.inseason.matchup import Array, Simulation


def term_interval(
    lower: Array, upper: Array, terms: tuple[Term, ...], axes: tuple[str, ...]
) -> tuple[Array, Array]:
    indices = [axes.index(t.stat_id) for t in terms]
    result = linear_interval(
        {
            "lower": lower,
            "upper": upper,
            "indices": np.asarray(indices, dtype=np.float64),
            "weights": np.asarray([t.coefficient for t in terms]),
        }
    )
    return result[0], result[1]


def score_ceiling(
    sim: Simulation, lower: Array, upper: Array, opponent: Array, *, week_tie: float = 0.0
) -> float:
    return float(score_ceilings(sim, lower, upper, opponent, week_tie=week_tie))


def score_ceilings(
    sim: Simulation, lower: Array, upper: Array, opponent: Array, *, week_tie: float = 0.0
) -> Array:
    """The same ceiling, allowing leading independent candidate dimensions."""
    lower, upper = np.maximum(lower, 0), np.maximum(upper, 0)
    away = category_values(opponent, sim.league.categories, sim.axes)
    differences: list[Array] = []
    for i, category in enumerate(sim.league.categories):
        formula = category.formula
        low, high = term_interval(
            lower,
            upper,
            formula.terms if isinstance(formula, Linear) else formula.numerator,
            sim.axes,
        )
        if not isinstance(formula, Linear):
            dlow, dhigh = term_interval(lower, upper, formula.denominator, sim.axes)
            bounds = ratio_interval({"lower": low, "upper": high, "dlower": dlow, "dupper": dhigh})
            low, high = bounds[0], bounds[1]
        ceiling = high if category.direction == "higher" else -low
        differences.append(
            comparison_margin(
                {
                    "home": ceiling,
                    "away": away[..., i],
                    "decimals": np.asarray(category.comparison_decimals),
                }
            )
        )
    margins = np.stack(differences, axis=-1)
    if sim.league.scoring == "h2h_each_category":
        ties = np.array(
            [
                c.tie_value if sim.league.category_ties == "use_tie_value" else 0.0
                for c in sim.league.categories
            ]
        )
        scores = category_points({"differences": margins, "ties": ties}).sum(axis=-1)
    else:
        scores = week_points({"differences": margins, "week_tie": np.asarray(week_tie)})
    raw = mean_array(scores, axis=-1)
    return np.array([sim.calibrated_score(float(v)).result for v in raw.flat]).reshape(raw.shape)


def certified_loss(
    sim: Simulation,
    home: str,
    away: str,
    week_id: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """Certify strict loss over all legal weekly lineups."""
    if (
        sim.league.scoring != "h2h_one_win"
        or sim.params.week_calibration.value <= 0
        or sim.transitions.get(home)
        or (
            sim.project_injury_returns
            and next(t.injury_players for t in sim.snapshot.teams if t.id == home)
        )
        or any(
            not isinstance(c.formula, Linear) and c.formula.zero_denominator == "error"
            for c in sim.league.categories
        )
    ):
        return False
    minimum = sim.calibrated_score(0).result
    if sim.calibrated_score(1 / sim.samples).result <= minimum:
        # Tiny positive calibration can round a nonzero probability to the
        # same scalar as zero. That scalar cannot certify standings draws.
        return False
    changed = rosters or {}
    actual, through = sim.actual(home, week_id)
    lower, upper = actual.copy(), actual.copy()
    opponent, _ = sim.total(away, week_id, changed.get(away))
    week = next(w for w in sim.league.matchups if w.id == week_id)
    on = max(week.start, sim.as_of.astimezone(sim.zone).date())
    roster = changed.get(home, sim.roster(home))
    while on <= week.end:
        sim.check_limits()
        for draw in sim.daily_draws(roster, on, max(through, sim.as_of)).values():
            lower = np.nextafter(lower + np.minimum(draw, 0), -np.inf)
            upper = np.nextafter(upper + np.maximum(draw, 0), np.inf)
        lower, upper = np.nextafter(lower, -np.inf), np.nextafter(upper, np.inf)
        on += timedelta(days=1)
    # Treat a tie as a full win here: a floor result then proves that no
    # sample can even tie. Both teams' standings points are therefore known.
    return score_ceiling(sim, lower, upper, opponent, week_tie=1.0) == minimum


def assignment_ceiling(
    sim: Simulation,
    draws: dict[str, Array],
    fixed: tuple[str, ...],
    free: tuple[str, ...],
    rest: Array,
    opponent: Array,
    *,
    achieved: float | None = None,
) -> float | None:
    if any(
        not isinstance(c.formula, Linear) and c.formula.zero_denominator == "error"
        for c in sim.league.categories
    ):
        # Preserve errors from every legal candidate, including the empty
        # assignment. A winning nonempty choice must not hide an unavailable
        # category calculation elsewhere in the original complete search.
        return None
    scale = 1.0 if sim.league.scoring == "h2h_one_win" else float(len(sim.league.categories))
    global_maximum = sim.calibrated_score(scale).result
    if achieved is not None and achieved >= global_maximum:
        # The preferred legal lineup already won every sample. No draw interval
        # can tighten this universal score ceiling enough to change the result.
        return global_maximum
    lower, upper = np.zeros_like(rest), np.zeros_like(rest)
    for player in (*fixed, *sorted(free)):
        draw = draws[player]
        low, high = (draw, draw) if player in fixed else (np.minimum(draw, 0), np.maximum(draw, 0))
        lower = np.nextafter(lower + low, -np.inf)
        upper = np.nextafter(upper + high, np.inf)
    return score_ceiling(
        sim,
        np.nextafter(rest + lower, -np.inf),
        np.nextafter(rest + upper, np.inf),
        opponent,
    )
