"""Independent whole-week oracles beyond the direct-enumeration threshold."""

from datetime import timedelta
from itertools import product

import numpy as np
import pytest
from inseason_support import fixture

from fba.contracts.config import Matchup, StarterSlot
from fba.contracts.inseason import CalculationTimeout, SeasonGame
from fba.inseason.matchup import Simulation


def quality_simulation(case, mode="h2h_one_win", days=4):
    args = list(fixture(mode))
    now = args[-1]
    args[0] = args[0].model_copy(
        update={
            "starter_slots": (StarterSlot(id="any", label="Any", eligible_positions=("PG", "C")),),
            "bench_slots": 2,
            "categories": tuple(
                c.model_copy(update={"comparison_decimals": 3}) if c.id in ("FG%", "FT%") else c
                for c in args[0].categories
            ),
            "matchups": (
                Matchup(
                    id="2",
                    start=now.date(),
                    end=now.date() + timedelta(days=days - 1),
                    phase="regular",
                ),
            ),
        }
    )
    games = tuple(
        SeasonGame(
            id=f"quality:{pid}:{day}",
            home=f"NBA{pid}",
            away="VISITOR",
            tipoff=now + timedelta(days=day, hours=12),
            known_at=now - timedelta(days=1),
            status="scheduled",
        )
        for pid in range(6)
        for day in range(days)
    )
    args[2] = args[2].model_copy(update={"games": games})
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"selected_slots": {"any": t.players[0]}})
                for t in args[5].teams
            )
        }
    )
    sim = Simulation(*args)
    install_draws(sim, case)
    return sim


def install_draws(sim, case):
    rng = np.random.default_rng(12000 + case)
    for game in sim.players.games:
        draws = rng.poisson(4, (sim.samples, len(sim.axes))).astype(float)
        for made, attempted in (("FGM", "FGA"), ("FTM", "FTA"), ("3PM", "FGM")):
            draws[:, sim.axes.index(made)] = np.minimum(
                draws[:, sim.axes.index(made)], draws[:, sim.axes.index(attempted)]
            )
        draws[:, sim.axes.index("PTS")] = (
            2 * draws[:, sim.axes.index("FGM")]
            + draws[:, sim.axes.index("FTM")]
            + draws[:, sim.axes.index("3PM")]
        )
        sim.draws[f"p{game.home[3:]}", game.id] = draws


def independent_score(own, other, axes, mode):
    margins = [
        own[:, axes.index(stat)] - other[:, axes.index(stat)]
        for stat in ("PTS", "REB", "AST", "STL", "BLK", "3PM")
    ]
    margins.append(other[:, axes.index("TO")] - own[:, axes.index("TO")])
    for made, attempted in (("FGM", "FGA"), ("FTM", "FTA")):
        rates = []
        for total in (own, other):
            num, den = (total[:, axes.index(s)] for s in (made, attempted))
            rates.append(np.divide(num, den, out=np.zeros_like(num), where=den != 0).round(3))
        margins.append(rates[0] - rates[1])
    votes = np.stack(margins, axis=-1)
    if mode == "h2h_one_win":
        return float((np.sign(votes).sum(axis=-1) > 0).mean())
    return float(((votes > 0) + 0.5 * (votes == 0)).sum(axis=-1).mean())


def exhaustive_answer(sim, other, options):
    answers = []
    for choices in product(*options):
        total = sum(
            (sim.draws[pid, f"quality:{pid[1:]}:{day}"] for day, pid in enumerate(choices) if pid),
            start=np.zeros_like(other),
        )
        value = independent_score(total, other, sim.axes, sim.league.scoring)
        # Independent documented tie order: most starters, then player IDs by day.
        key = (
            -sum(pid is not None for pid in choices),
            tuple((pid,) if pid else () for pid in choices),
        )
        answers.append((value, key, total, choices))
    return min(answers, key=lambda row: (-row[0], row[1]))


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("case", range(30))
def test_four_day_search_matches_all_256_legal_combinations(case, mode):
    sim = quality_simulation(case, mode)
    assert sim.params.weekly_exact_candidates.value == 64
    other, _ = sim.total("team1", "2")
    expected, _, total, choices = exhaustive_answer(sim, other, [(None, "p0", "p1", "p2")] * 4)
    forecast = sim.week("team0", "team1", "2")
    own, actual_days = sim.optimize_total("team0", "2", other)
    assert forecast.lineup_search == "joint_exact"
    assert forecast.raw_score == pytest.approx(expected, abs=1e-12, rel=0)
    assert tuple(day.slots.get("any") for day in actual_days) == choices
    np.testing.assert_array_equal(own, total)
    if case == 12 and mode == "h2h_one_win":
        assert expected == 0.55
        assert choices == ("p0", "p0", "p2", "p0")


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_six_day_search_crosses_multiple_branch_levels(mode):
    sim = quality_simulation(41, mode, days=6)
    other, _ = sim.total("team1", "2")
    expected, _, _, choices = exhaustive_answer(sim, other, [(None, "p0", "p1", "p2")] * 6)
    own, days = sim.optimize_total("team0", "2", other)
    assert independent_score(own, other, sim.axes, mode) == expected
    assert tuple(day.slots.get("any") for day in days) == choices


@pytest.mark.parametrize("interruption", ("deadline", "cancelled"))
def test_interrupted_weekly_search_publishes_no_forecast_or_exact_certificate(
    monkeypatch, interruption
):
    import fba.inseason.weekly_lineups as search

    sim = quality_simulation(12)
    original = search.completion_ceiling
    count = 0

    def interrupt(*args):
        nonlocal count
        count += 1
        if interruption == "cancelled":
            sim.cancelled = lambda: True
        else:
            sim.deadline = (0.0, "week")
        return original(*args)

    monkeypatch.setattr(search, "completion_ceiling", interrupt)
    with pytest.raises(CalculationTimeout):
        sim.week("team0", "team1", "2")
    assert count == 1
    assert not sim.joint_weeks and not sim.matchup_cache and not sim.forecast_cache
    assert sim.deadline is None


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_locked_today_and_future_move_match_independent_roster_options(mode):
    from fba.contracts.inseason_results import RosterMove
    from fba.inseason.recommendations import changed_simulation

    sim = quality_simulation(12, mode)
    sim.league = sim.league.model_copy(update={"lineup_lock": "daily"})
    extra = tuple(
        g.model_copy(update={"id": f"quality:6:{i}", "home": "NBA6"})
        for i, g in enumerate(g for g in sim.games if g.home == "NBA0")
    )
    sim.players = sim.players.model_copy(update={"games": (*sim.games, *extra)})
    sim.games = sim.players.games
    for day in range(4):
        sim.draws["p6", f"quality:6:{day}"] = sim.draws["p2", f"quality:2:{day}"] * 1.125
    child = changed_simulation(
        sim,
        "team0",
        (
            RosterMove(
                add="p6",
                drop="p0",
                effective_on=sim.as_of.date() + timedelta(days=2),
                starter_games=0,
            ),
        ),
    )
    child.draws = sim.draws
    other, _ = child.total("team1", "2")
    options = [
        ("p0",),
        (None, "p0", "p1", "p2"),
        (None, "p1", "p2", "p6"),
        (None, "p1", "p2", "p6"),
    ]
    expected, _, total, choices = exhaustive_answer(child, other, options)
    own, days = child.optimize_total("team0", "2", other)
    assert independent_score(own, other, sim.axes, mode) == expected
    assert tuple(day.slots.get("any") for day in days) == choices
    np.testing.assert_array_equal(own, total)
    assert all("p0" not in day.bench for day in days[2:])


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_zero_calibration_uses_fullest_lexical_tie_order(mode):
    sim = quality_simulation(12, mode)
    sim.params = sim.params.model_copy(
        update={
            key: getattr(sim.params, key).model_copy(update={"value": 0.0})
            for key in ("calibration", "week_calibration")
        }
    )
    other, _ = sim.total("team1", "2")
    _, days = sim.optimize_total("team0", "2", other)
    assert tuple(day.slots.get("any") for day in days) == ("p0",) * 4


def test_error_on_zero_denominator_is_not_hidden_by_winning_incumbent():
    from fba.contracts.base import DataError
    from fba.inseason.weekly_lineups import joint_lineup

    sim = quality_simulation(12)
    other, _ = sim.total("team1", "2")
    _, initial = sim.total("team0", "2")
    _, through = sim.actual("team0", "2")
    category = next(c for c in sim.league.categories if c.id == "FG%")
    sim.league = sim.league.model_copy(
        update={
            "categories": (
                category.model_copy(
                    update={
                        "formula": category.formula.model_copy(update={"zero_denominator": "error"})
                    }
                ),
            )
        }
    )
    with pytest.raises(DataError, match="denominator"):
        joint_lineup(sim, "team0", "2", other, None, initial, through)
    assert not sim.joint_weeks


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("seed", range(6))
def test_completion_bound_covers_signed_floating_draws_and_every_legal_suffix(mode, seed):
    from fba.inseason.weekly_lineups import completion_ceiling, day_choices

    sim = quality_simulation(12, mode)
    rng = np.random.default_rng(seed)
    for key, draw in sim.draws.items():
        # Include negative and mixed-scale inputs: the scoring contract clamps
        # totals, and bounds must cover the same sums before that clamp.
        sim.draws[key] = rng.uniform(-3, 8, draw.shape) * rng.choice((1e-12, 1.0, 1e12), draw.shape)
    _, initial = sim.total("team0", "2")
    actual, through = sim.actual("team0", "2")
    choices = tuple(day_choices(sim, "team0", day, sim.roster("team0"), through) for day in initial)
    other, _ = sim.total("team1", "2")
    prefix = sim.draws["p0", "quality:0:0"]
    ceiling = completion_ceiling(sim, prefix, choices[1:], actual, other)
    for tail in product((None, "p0", "p1", "p2"), repeat=3):
        total = sum(
            (
                sim.draws[pid, f"quality:{pid[1:]}:{day}"]
                for day, pid in enumerate(tail, start=1)
                if pid
            ),
            start=prefix,
        )
        value = sim.calibrated_score(float(sim.score(actual + total, other)[1].mean())).result
        assert value <= ceiling + 1e-12
