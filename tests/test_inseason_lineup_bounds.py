"""Compare numeric ceilings and early exits with complete legal choices."""

from itertools import combinations
from time import monotonic

import numpy as np
import pytest
from inseason_support import simulation

from fba.contracts.base import DataError
from fba.contracts.config import Linear, Ratio, Term
from fba.core.lineups import best_lineup, legal_assignment
from fba.inseason.lineup_bounds import assignment_ceiling


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("seed", range(8))
def test_ceiling_bounds_all_signed_ratio_and_rounded_subset_scores(mode, seed):
    sim = simulation(mode=mode)
    rng = np.random.default_rng(seed)
    categories = list(sim.league.categories)
    categories[0] = categories[0].model_copy(
        update={
            "formula": Linear(
                kind="linear",
                terms=(Term(stat_id="PTS", coefficient=2.0), Term(stat_id="TO", coefficient=-3.0)),
            ),
            "comparison_decimals": 12,
            "direction": "lower" if seed % 2 else "higher",
        }
    )
    categories[6] = categories[6].model_copy(
        update={
            "formula": Ratio(
                kind="ratio",
                numerator=(
                    Term(stat_id="FGM", coefficient=-2.0),
                    Term(stat_id="AST", coefficient=1.0),
                ),
                denominator=(Term(stat_id="FGA", coefficient=1.0),),
                zero_denominator="numerator" if seed % 2 else "zero",
            ),
            "direction": "lower" if seed % 2 else "higher",
        }
    )
    sim.league = sim.league.model_copy(update={"categories": tuple(categories)})
    shape = (sim.samples, len(sim.axes))
    draws = {f"p{i}": rng.uniform(-3, 10, shape) for i in range(5)}
    rest, other = rng.uniform(-2, 20, shape), rng.uniform(0, 60, shape)
    positions = {p: ("PG", "C") for p in draws}
    legal = [
        row
        for n in range(3)
        for row in combinations(sorted(draws), n)
        if legal_assignment(sim.league.starter_slots, positions, row) is not None
    ]

    def score(row):
        return sim.calibrated_score(
            float(
                sim.score(rest + sum((draws[p] for p in row), start=np.zeros_like(rest)), other)[
                    1
                ].mean()
            )
        ).result

    maximum = assignment_ceiling(sim, draws, (), tuple(draws), rest, other)
    assert maximum is not None and all(score(row) <= maximum for row in legal)
    plain = best_lineup(sim.league.starter_slots, positions, score, sim.params.tolerance.value)
    fast = best_lineup(
        sim.league.starter_slots, positions, score, sim.params.tolerance.value, maximum=maximum
    )
    assert fast == plain
    lazy = best_lineup(
        sim.league.starter_slots,
        positions,
        score,
        sim.params.tolerance.value,
        maximum=lambda achieved: assignment_ceiling(
            sim, draws, (), tuple(draws), rest, other, achieved=achieved
        ),
    )
    assert lazy == plain


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_early_exit_preserves_fullest_lexical_tie_and_saves_actual_objective_calls(mode):
    sim = simulation(mode=mode)
    shape = (sim.samples, len(sim.axes))
    draws = {p: np.zeros(shape) for p in ("p0", "p1", "p2")}
    rest, other = np.ones(shape), np.ones(shape) * 100
    calls = []

    def score(ids):
        calls.append(ids)
        return sim.calibrated_score(float(sim.score(rest, other)[1].mean())).result

    positions = dict.fromkeys(draws, ("PG", "C"))
    maximum = assignment_ceiling(sim, draws, (), tuple(draws), rest, other)
    expected = best_lineup(sim.league.starter_slots, positions, score, sim.params.tolerance.value)
    exhaustive_calls = len(calls)
    calls.clear()
    actual = best_lineup(
        sim.league.starter_slots, positions, score, sim.params.tolerance.value, maximum=maximum
    )
    assert actual == expected
    assert tuple(sorted(actual[0].values())) == ("p0", "p1")
    assert len(calls) == 1 < exhaustive_calls


def test_ratio_zero_denominator_error_is_not_hidden_by_nonempty_ceiling_choice():
    sim = simulation()
    cats = tuple(
        c.model_copy(update={"formula": c.formula.model_copy(update={"zero_denominator": "error"})})
        if isinstance(c.formula, Ratio)
        else c
        for c in sim.league.categories
    )
    sim.league = sim.league.model_copy(update={"categories": cats})
    on = sim.as_of.astimezone(sim.zone).date()
    shape = (sim.samples, len(sim.axes))
    draws = {"p0": np.ones(shape)}
    assert (
        assignment_ceiling(
            sim, draws, (), tuple(draws), np.zeros(shape), np.ones(shape), achieved=1.0
        )
        is None
    )
    with pytest.raises(DataError, match="zero denominator"):
        sim.optimize_assignment("team0", on, draws, np.zeros(shape), np.ones(shape), monotonic())


def test_bounded_lineup_batch_length_error_remains_explicit():
    sim = simulation()
    with pytest.raises(DataError, match="length differs"):
        best_lineup(
            sim.league.starter_slots,
            {"p0": ("PG",)},
            lambda _: 1.0,
            1e-9,
            batch_objective=lambda _: (),
            maximum=1.0,
        )


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_global_winning_score_does_not_need_a_more_expensive_interval(monkeypatch, mode):
    from fba.inseason import lineup_bounds

    sim = simulation(mode=mode)
    scale = 1.0 if mode == "h2h_one_win" else float(len(sim.league.categories))
    maximum = sim.calibrated_score(scale).result
    shape = (sim.samples, len(sim.axes))

    def unnecessary_interval(*_):
        raise AssertionError("global maximum already bounds every legal lineup")

    monkeypatch.setattr(lineup_bounds, "term_interval", unnecessary_interval)
    assert (
        assignment_ceiling(sim, {}, (), (), np.zeros(shape), np.zeros(shape), achieved=maximum)
        == maximum
    )


@pytest.mark.parametrize("shape", ((13,), (40, 13), (200, 13)))
@pytest.mark.parametrize("fixed", ((), ("p3", "p1")))
def test_ordered_prefix_sums_preserve_signed_values_and_input_arrays(shape, fixed):
    from fba.inseason.lineup_space import ordered_row_sums

    rng = np.random.default_rng(204)
    values = {p: rng.uniform(-1e10, 1e10, shape) for p in ("p3", "p0", "p2", "p1")}
    for array in values.values():
        array.flat[0] = -0.0
    rows = ((), ("p2",), ("p1", "p0"), ("p0", "p1", "p2"))
    for choices in (rows, rows[:1], rows[1:2]):
        expected = np.stack(
            [sum((values[p] for p in (*fixed, *row)), start=np.zeros(shape)) for row in choices]
        )
        actual = ordered_row_sums(choices, values, shape, fixed)
        assert np.array_equal(actual, expected)
        assert np.array_equal(np.signbit(actual), np.signbit(expected))
        rest = rng.uniform(-1e5, 1e5, shape)
        assert np.array_equal(rest + actual, rest + expected)
        actual.fill(-1)
        assert values["p0"].flat[0] == 0
