"""Independent legal-lineup and distribution oracles for count-coupled samples."""

from collections import Counter
from datetime import timedelta
from fractions import Fraction
from itertools import product

import numpy as np
import pytest
from test_inseason_weekly_quality import quality_simulation

from fba.contracts.config import StarterSlot
from fba.contracts.inseason import CalculationTimeout
from fba.inseason.count_feasibility import CountFeasibility
from fba.inseason.sampling import sampling_profile
from fba.inseason.weekly_counts import CountSearch
from fba.inseason.weekly_lineups import WeeklySearch, day_choices
from fba.inseason.weekly_samples import CountSamples


def generated_simulation(mode="h2h_one_win", days=4):
    sim = quality_simulation(0, mode, days)
    sim.draws.clear()
    return sim


def count_problem(sim):
    other, _ = sim.total("team1", "2")
    actual, through = sim.actual("team0", "2")
    _, initial = sim.total("team0", "2")
    days = tuple(day_choices(sim, "team0", day, sim.roster("team0"), through) for day in initial)
    samples = CountSamples.create(sim, tuple((day.on, day.draws) for day in days), through)
    assert samples is not None
    return WeeklySearch(sim, days, actual, other), samples


def independent_count_total(sim, choices):
    # All players share each day's fixture projection. Count independent game
    # draws in chronological order, irrespective of the selected dates.
    counts = Counter(pid for pid in choices if pid is not None)
    total = np.zeros((sim.samples, len(sim.axes)))
    for pid in ("p0", "p1", "p2"):
        contribution = np.zeros_like(total)
        for day in range(counts[pid]):
            contribution = contribution + sim.draws[pid, f"quality:{pid[1:]}:{day}"]
        total = total + contribution
    return total


def count_score(sim, own, other):
    # Generated bootstrap boxes can be fractional: all categories use their
    # declared comparison precision, including counting stats (six decimals).
    margins = []
    precision = {c.id: c.comparison_decimals for c in sim.league.categories}
    for stat in ("PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"):
        a, b = (v[:, sim.axes.index(stat)].round(precision[stat]) for v in (own, other))
        margins.append(b - a if stat == "TO" else a - b)
    for stat, made, attempted in (("FG%", "FGM", "FGA"), ("FT%", "FTM", "FTA")):
        rates = []
        for total in (own, other):
            n, d = (total[:, sim.axes.index(s)] for s in (made, attempted))
            rates.append(np.divide(n, d, out=np.zeros_like(n), where=d != 0).round(precision[stat]))
        margins.append(rates[0] - rates[1])
    votes = np.stack(margins, axis=-1)
    if sim.league.scoring == "h2h_one_win":
        return float((np.sign(votes).sum(axis=-1) > 0).mean())
    return float(((votes > 0) + 0.5 * (votes == 0)).sum(axis=-1).mean())


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("seed", range(8))
def test_generated_count_search_matches_every_legal_lineup(mode, seed):
    sim = generated_simulation(mode)
    sim.params = sim.params.model_copy(
        update={"seed": sim.params.seed.model_copy(update={"value": seed + 900})}
    )
    search, samples = count_problem(sim)
    answers = []
    for choices in product((None, "p0", "p1", "p2"), repeat=4):
        total = independent_count_total(sim, choices)
        score = count_score(sim, total, search.opponent)
        key = (-sum(p is not None for p in choices), tuple((p,) if p else () for p in choices))
        answers.append((score, key, total, choices))
    expected = min(answers, key=lambda row: (-row[0], row[1]))
    actual, rows = CountSearch.create(search, samples).run()
    assert tuple(row.get("any") for row in rows) == expected[3]
    np.testing.assert_array_equal(actual, expected[2])
    forecast = sim.week("team0", "team1", "2")
    assert forecast.raw_score == pytest.approx(expected[0], abs=1e-12, rel=0)
    assert forecast.sample_coupling == {
        "team0": "exchangeable_count_v1",
        "team1": "exchangeable_count_v1",
    }


def test_count_flow_matches_all_legal_products_and_canonical_ties():
    sim = generated_simulation()
    search, samples = count_problem(sim)
    legal = CountFeasibility.create(search, samples)
    expected = {}
    for rows in product(*(day.rows for day in search.days)):
        counts = samples.counts(rows)
        key = tuple(tuple(sorted(row.values())) for row in rows)
        if counts not in expected or key < expected[counts][0]:
            expected[counts] = key, rows
    for counts in product(range(5), repeat=len(samples.choices)):
        assert legal.feasible(np.array(counts), 0) == (counts in expected)
        result = legal.canonical(counts)
        assert result == (expected[counts][1] if counts in expected else None)


def test_count_coupling_preserves_exact_iid_distribution_for_every_fixed_subset():
    # Enumerate the entire joint probability space (not a statistical tolerance):
    # three independent Bernoulli games, p=1/3, any two selected dates vs prefix.
    totals = [Counter(), Counter(), Counter()]
    for outcomes in product((0, 1), repeat=3):
        mass = Fraction(1)
        for value in outcomes:
            mass *= Fraction(1, 3) if value else Fraction(2, 3)
        for i, indices in enumerate(((0, 1), (0, 2), (1, 2))):
            totals[i][sum(outcomes[j] for j in indices)] += mass
    assert totals == [Counter({0: Fraction(4, 9), 1: Fraction(4, 9), 2: Fraction(1, 9)})] * 3


def test_equal_counts_ignore_date_and_slot_order_but_keep_independent_games():
    sim = generated_simulation()
    search, samples = count_problem(sim)
    a = ({"any": "p0"}, {}, {"any": "p0"}, {})
    b = ({}, {"any": "p0"}, {}, {"any": "p0"})
    first = samples.total(search.actual, samples.counts(a))
    np.testing.assert_array_equal(first, samples.total(search.actual, samples.counts(b)))
    np.testing.assert_array_equal(
        first, sim.draws["p0", "quality:0:0"] + sim.draws["p0", "quality:0:1"]
    )
    assert not np.array_equal(sim.draws["p0", "quality:0:0"], sim.draws["p0", "quality:0:1"])


def test_distribution_identity_separates_players_rates_minutes_and_availability():
    sim = generated_simulation()
    player = sim.projection(sim.as_of.date()).players[0]
    original = sampling_profile(player)
    for changes in (
        {"minutes": player.minutes + 1},
        {"probability": player.probability / 2},
        {"rates": {**player.rates, "FGM": player.rates["FGM"] + 0.1}},
        {"player": player.player.model_copy(update={"id": "different"})},
    ):
        assert sampling_profile(player.model_copy(update=changes)) != original


def test_a_future_projection_change_splits_the_actual_sampling_pool():
    sim = generated_simulation()
    on = sim.as_of.date() + timedelta(days=1)
    projection = sim.projection(on)
    sim.projections[on] = projection.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"minutes": p.minutes + 5}) if p.player.id == "p0" else p
                for p in projection.players
            )
        }
    )
    search, samples = count_problem(sim)
    original = samples.by_day[0]["p0"]
    changed = samples.by_day[1]["p0"]
    assert changed != original == samples.by_day[2]["p0"] == samples.by_day[3]["p0"]
    rows = ({"any": "p0"}, {}, {"any": "p0"}, {})
    np.testing.assert_array_equal(
        samples.total(search.actual, samples.counts(rows)),
        sim.draws["p0", "quality:0:0"] + sim.draws["p0", "quality:0:2"],
    )


def test_unverified_or_replaced_scenario_arrays_keep_game_id_policy():
    sim = quality_simulation(12)
    assert sim.week("team0", "team1", "2").sample_coupling["team0"] == "game_id"
    sim = generated_simulation()
    search, _ = count_problem(sim)
    key = "p0", "quality:0:0"
    sim.draws[key] = sim.draws[key].copy()
    assert (
        CountSamples.create(sim, tuple((day.on, day.draws) for day in search.days), sim.as_of)
        is None
    )


def test_multi_game_day_retains_complete_appearance_search():
    sim = generated_simulation(days=2)
    first = sim.games[0]
    sim.games = (
        *sim.games,
        first.model_copy(update={"id": "second", "tipoff": first.tipoff + timedelta(hours=1)}),
    )
    forecast = sim.week("team0", "team1", "2")
    assert forecast.lineup_search == "joint_exact"
    assert forecast.sample_coupling["team0"] == "game_id"


def test_interrupted_count_search_publishes_no_forecast_or_completion():
    sim = generated_simulation()
    search, samples = count_problem(sim)
    engine = CountSearch.create(search, samples)
    sim.cancelled = lambda: True
    with pytest.raises(CalculationTimeout):
        engine.run()
    assert not sim.forecast_cache
    assert not sim.joint_weeks


def test_position_min_cuts_are_valid_for_every_complete_and_partial_count():
    sim = generated_simulation(days=3)
    sim.league = sim.league.model_copy(
        update={
            "starter_slots": (
                StarterSlot(id="pg", label="PG", eligible_positions=("PG",)),
                StarterSlot(id="c", label="C", eligible_positions=("C",)),
            )
        }
    )
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"positions": ("C",) if p.id == "p2" else ("PG",)})
                for p in sim.players.players
            )
        }
    )
    search, samples = count_problem(sim)
    legal = CountFeasibility.create(search, samples)
    assert not legal.feasible(np.array([2, 2, 0]), 0)
    assert legal.cuts == {(0, 1): 3}
    possible = {samples.counts(rows) for rows in product(*(day.rows for day in search.days))}
    for counts in product(range(4), repeat=3):
        assert legal.feasible(np.array(counts), 0) == (counts in possible)
    for length in range(4):
        for prefix in product(range(4), repeat=length):
            if any(counts[:length] == prefix for counts in possible):
                assert legal.prefix_possible((0, 1, 2), prefix)


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_today_counterfactuals_recompose_count_prefixes(mode):
    from fba.inseason.today import lineup_effects

    sim = generated_simulation(mode)
    own, other, days = sim.matchup_totals("team0", "team1", "2")
    rows = tuple(d for d in days if d.team_id == "team0")
    day = rows[0]
    effects = lineup_effects(sim, day, "team1", "2")
    scale = (
        sim.params.week_calibration.value if mode == "h2h_one_win" else sim.params.calibration.value
    )
    chosen = tuple(d.slots.get("any") for d in rows)
    base = count_score(sim, own, other)
    for pid, (trace, _) in effects.items():
        alternatives = [
            count_score(sim, independent_count_total(sim, (choice, *chosen[1:])), other)
            for choice in (None, "p0", "p1", "p2")
            if (choice != pid if pid == chosen[0] else choice == pid)
        ]
        assert trace.result == pytest.approx((base - max(alternatives)) * scale, abs=1e-12)
