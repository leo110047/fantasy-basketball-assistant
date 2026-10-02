"""Independent complete subsets and adversarial floating point comparisons."""

from itertools import combinations, permutations, product

import numpy as np
import pytest
from inseason_support import simulation

from fba.contracts.config import Linear, Ratio, Term
from fba.formulas.categories import scoring_differences
from fba.formulas.lineup import comparison_thresholds, linear_roundoff, threshold_votes
from fba.inseason.weekly_bounds import ThresholdBounds
from fba.inseason.weekly_players import add_bound


@pytest.mark.parametrize("decimals", (0, 3, 12))
def test_thresholds_cannot_exclude_any_rounded_tie_or_win(decimals):
    away = np.array([-1e50, -1.25, -0.0005, 0, 0.0005, 0.45, 1.25, 1e50])
    thresholds = comparison_thresholds({"away": away, "decimals": np.asarray(decimals)})
    target = np.round(away, decimals)
    assert np.all(np.round(thresholds[:, 0], decimals) < target)
    assert np.all(np.round(thresholds[:, 1], decimals) <= target)
    for direction in (-np.inf, np.inf):
        for boundary in thresholds.T:
            for home in (boundary, np.nextafter(boundary, direction)):
                result = np.round(home, decimals) - target
                assert np.all((result < 0) | (home > thresholds[:, 0]))
                assert np.all((result <= 0) | (home > thresholds[:, 1]))


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("scale", (1e-80, 1.0, 1e80))
def test_correlated_ceiling_encloses_every_subset_and_order(mode, seed, scale):
    sim = simulation(mode=mode)
    rng = np.random.default_rng(8100 + seed)
    cats = list(sim.league.categories)
    cats[0] = cats[0].model_copy(
        update={
            "formula": Linear(
                kind="linear",
                terms=(
                    Term(stat_id="PTS", coefficient=1.125),
                    Term(stat_id="TO", coefficient=-2.5),
                ),
            ),
            "direction": "lower" if seed % 2 else "higher",
            "comparison_decimals": 12,
        }
    )
    cats[6] = cats[6].model_copy(
        update={
            "formula": Ratio(
                kind="ratio",
                numerator=(
                    Term(stat_id="FGM", coefficient=-1.125),
                    Term(stat_id="AST", coefficient=1.0),
                ),
                denominator=(Term(stat_id="FGA", coefficient=2.5),),
                zero_denominator="zero",
            ),
            "direction": "lower" if seed % 2 else "higher",
            "comparison_decimals": seed % 4,
        }
    )
    sim.league = sim.league.model_copy(update={"categories": tuple(cats)})
    shape = (sim.samples, len(sim.axes))
    draws = tuple(rng.uniform(0, 10, shape) * scale for _ in range(4))
    actual, other = rng.uniform(0, 3, shape) * scale, rng.uniform(0, 30, shape) * scale
    # Exercise legitimate zero denominators, even with nonzero numerators.
    for a in (*draws, actual, other):
        a[:10, sim.axes.index("FGA")] = 0
    bounds = ThresholdBounds.create(sim, actual, other, draws)
    assert bounds is not None
    margins = tuple(bounds.contribution(a) for a in draws)
    origin = bounds.contribution(actual, origin=True)
    for included in product((False, True), repeat=len(draws)):
        ids = tuple(i for i, chosen in enumerate(included) if chosen)
        upper = origin
        for i in ids:
            upper = add_bound(upper, margins[i])
        votes = threshold_votes(
            {"upper": upper, "error": bounds.error, "zero_votes": bounds.zero_votes}
        )
        for order in permutations(ids):
            total = actual + sum((draws[i] for i in order), start=np.zeros_like(actual))
            expected = np.sign(scoring_differences(total, other, sim.league.categories, sim.axes))
            assert np.all(expected <= votes)
        # An unassigned remainder must cover all its completions as well.
        relaxed = origin
        for margin in margins:
            relaxed = add_bound(relaxed, margin, fixed=False)
        assert bounds.ceiling(upper) <= bounds.ceiling(relaxed)


@pytest.mark.parametrize("kind", ("negative_draw", "negative_denominator", "numerator", "error"))
def test_unsupported_bounds_leave_the_existing_complete_search_available(kind):
    sim = simulation()
    actual = np.ones((sim.samples, len(sim.axes)))
    draws = [actual.copy()]
    if kind == "negative_draw":
        draws[0][0, 0] = -1
    else:
        cats = list(sim.league.categories)
        formula = cats[6].formula
        update = {"zero_denominator": kind}
        if kind == "negative_denominator":
            update = {"denominator": (Term(stat_id="FGA", coefficient=-1),)}
        cats[6] = cats[6].model_copy(update={"formula": formula.model_copy(update=update)})
        sim.league = sim.league.model_copy(update={"categories": tuple(cats)})
    assert ThresholdBounds.create(sim, actual, actual, tuple(draws)) is None


def test_roundoff_covers_cancellation_subnormals_and_changed_accumulation_order():
    from fractions import Fraction

    terms = np.array([1.125, -2.5])
    values = (np.array([1e80, 1e80]), np.array([1.0, 2.0]), np.array([1e-300, 5e-324]))
    upper = sum(values) * 1.01
    error = float(
        linear_roundoff(
            {
                "upper": upper,
                "additions": np.asarray(4.0),
                "weights": terms,
                "indices": np.array([0.0, 1.0]),
            }
        )
    )
    exact = sum(
        Fraction(float(a[i])) * Fraction(float(w)) for a in values for i, w in enumerate(terms)
    )
    for order in permutations(values):
        total = sum(order, start=np.zeros(2))
        calculated = sum(float(x * w) for x, w in zip(total, terms, strict=True))
        assert abs(Fraction(calculated) - exact) <= Fraction(error)


def test_mutually_exclusive_positions_match_exhaustive_week():
    from test_inseason_weekly_quality import independent_score, quality_simulation

    from fba.contracts.config import StarterSlot
    from fba.inseason.weekly_lineups import day_choices

    sim = quality_simulation(53, days=3)
    sim.league = sim.league.model_copy(
        update={
            "starter_slots": (
                StarterSlot(id="guard", label="Guard", eligible_positions=("PG",)),
                StarterSlot(id="big", label="Big", eligible_positions=("C",)),
            ),
            "bench_slots": 1,
        }
    )
    other, _ = sim.total("team1", "2")
    _, initial = sim.total("team0", "2")
    _, through = sim.actual("team0", "2")
    days = tuple(day_choices(sim, "team0", day, sim.roster("team0"), through) for day in initial)
    # Independently list every legal player set, then retain canonical slot
    # addition order for the exact binary64 total returned by the application.
    sets = ((), ("p0",), ("p1",), ("p2",), *combinations(("p0", "p1", "p2"), 2))
    answers = []
    for choices in product(sets, repeat=3):
        total = np.zeros_like(other)
        for day, chosen in zip(days, choices, strict=True):
            row = next(r for r in day.rows if set(r.values()) == set(chosen))
            for pid in row.values():
                total = total + day.draws[pid]
        score = independent_score(total, other, sim.axes, sim.league.scoring)
        answers.append((score, (-sum(map(len, choices)), choices), total))
    expected = min(answers, key=lambda a: (-a[0], a[1]))
    own, result = sim.optimize_total("team0", "2", other)
    assert tuple(tuple(sorted(d.slots.values())) for d in result) == expected[1][1]
    np.testing.assert_array_equal(own, expected[2])


def test_score_ceiling_includes_every_sample():
    sim = simulation()
    actual = np.zeros((sim.samples, len(sim.axes)))
    opponent = np.ones_like(actual) * 100
    bounds = ThresholdBounds.create(sim, actual, opponent, ())
    assert bounds is not None
    upper = np.full_like(bounds.error, -1000.0)
    upper[64:] = 1000.0
    bounds.zero_votes[:] = -1.0
    assert bounds.ceiling(upper) == sim.calibrated_score(36 / 100).result


def test_fixed_accumulation_allowance_covers_all_signed_orders():
    from fractions import Fraction

    from fba.formulas.lineup import accumulation_error, threshold_accumulate

    values = (1e80, -1e80, 1.0, -0.1, 5e-324)
    magnitude = np.asarray(sum(abs(v) for v in values) * 1.01)
    error = float(accumulation_error({"magnitude": magnitude, "additions": np.asarray(7.0)}))
    exact = sum(Fraction(v) for v in values)
    for order in permutations(values):
        actual = np.asarray(0.0)
        for value in order:
            actual = threshold_accumulate({"base": actual, "draw": np.asarray(value)})
        assert abs(Fraction(float(actual)) - exact) <= Fraction(error)
