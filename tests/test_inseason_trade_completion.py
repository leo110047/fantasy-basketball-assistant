"""A relaxed draw envelope may skip only losing free-agent completion choices."""

from datetime import timedelta
from time import monotonic

import numpy as np
import pytest
from inseason_support import fixture

from fba.contracts.base import DataError
from fba.contracts.config import Linear, Ratio, Term
from fba.contracts.inseason import CalculationTimeout, SeasonGame
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import legal_roster
from fba.inseason.season import season_value
from fba.inseason.trade_bounds import free_agent_bounds
from fba.inseason.trades import completion_candidates


def completion_case(mode="h2h_one_win", *, loose=False):
    args = list(fixture(mode=mode))
    args[0] = args[0].model_copy(update={"categories": (args[0].categories[1],)})
    counts = []
    for day, phase in ((0, "current"), (2, "future"), (4, "future")):
        players = ["p0", "p1", "p3", "p4"]
        players += ["p6", "p7", "p8"] if day == 0 else ["p8"] if day == 2 else []
        for pid in players:
            value = 40 if pid in ("p0", "p1") else 90
            if pid == "p6":
                value = 5
            elif pid == "p7":
                value = 200
            elif pid == "p8":
                value = (200 if phase == "current" else 210) if loose else 100 if day == 0 else 200
            counts.append((day, pid, value))
    games = tuple(
        SeasonGame(
            id=f"completion:{day}:{pid}",
            home=f"NBA{pid[1:]}",
            away=f"visitor:{pid}",
            tipoff=args[-1] + timedelta(days=day, hours=12),
            known_at=args[-1] - timedelta(days=1),
            status="scheduled",
        )
        for day, pid, _ in counts
    )
    args[2] = args[2].model_copy(update={"games": games})
    sim = Simulation(*args, samples=args[1].season_simulations.value)
    # Recorded sample inputs at the engine's existing draw-cache seam.
    # The real lineup and scoring engines remain in use.
    for game, (_, pid, value) in zip(games, counts, strict=True):
        box = np.zeros((sim.samples, len(sim.axes)))
        box[:, sim.axes.index("REB")] = value
        sim.draws[pid, game.id] = box
    return sim


def complete_add_choice(sim, roster):
    on = sim.as_of.astimezone(sim.zone).date()
    values = [
        (season_value(sim, "team0", {"team0": (*roster, agent.player_id)}), agent.player_id)
        for agent in sim.snapshot.free_agents
        if agent.status == "free"
        and agent.player_id not in roster
        and legal_roster(sim, (*roster, agent.player_id), on)
    ]
    return min(values, key=lambda row: (-round(row[0] / sim.params.tolerance.value), row[1]))


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_proved_losing_add_does_not_require_its_full_season_forecast(mode):
    sim = completion_case(mode)
    roster, choices = ("p0", "p1"), ("p6", "p7", "p8")
    expected = complete_add_choice(completion_case(mode), roster)
    result = completion_candidates(
        sim, "team0", roster, choices, sim.as_of.astimezone(sim.zone).date(), True, None
    )
    assert expected[1] == "p7"
    assert min(result, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1])) == expected
    assert {p for _, p in result} == {"p6", "p7"}
    assert not any(key[3] == (*roster, "p8") for key in sim.matchup_cache)


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_an_earlier_id_tied_with_a_looser_bound_winner_is_still_fully_compared(mode):
    sim = completion_case(mode, loose=True)
    roster, choices = ("p0", "p1"), ("p6", "p7", "p8")
    bounds = free_agent_bounds(sim, "team0", roster, choices)
    assert bounds is not None and bounds["p8"] > bounds["p7"]
    expected = complete_add_choice(completion_case(mode, loose=True), roster)
    result = completion_candidates(
        sim, "team0", roster, choices, sim.as_of.astimezone(sim.zone).date(), True, None
    )
    assert [p for _, p in result] == ["p6", "p8", "p7"]
    assert min(result, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1])) == expected
    assert expected[1] == "p7"  # p8 scored first, but its tie does not change the winner.


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("joint", (True, False))
@pytest.mark.parametrize("seed", range(4))
def test_candidate_bounds_cover_full_forecasts_with_signed_terms_and_rounded_ratios(
    mode, joint, seed
):
    args = list(fixture(mode=mode))
    categories = list(args[0].categories)
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
                numerator=(Term(stat_id="AST", coefficient=-1.0),),
                denominator=(Term(stat_id="TO", coefficient=1.0),),
                zero_denominator="numerator" if seed % 2 else "zero",
            ),
            "comparison_decimals": 12,
        }
    )
    args[0] = args[0].model_copy(update={"categories": tuple(categories)})
    if not joint:
        args[1] = args[1].model_copy(
            update={
                "weekly_exact_candidates": args[1].weekly_exact_candidates.model_copy(
                    update={"value": 1}
                )
            }
        )
    sim = Simulation(*args, samples=args[1].season_simulations.value)
    rng = np.random.default_rng(seed)
    for player in sim.players.players:
        for game in sim.games:
            if player.team_id in (game.home, game.away):
                sim.draws[player.id, game.id] = rng.uniform(-8, 20, (sim.samples, len(sim.axes)))
    roster, choices = ("p0", "p1"), ("p6", "p7", "p8")
    bounds = free_agent_bounds(sim, "team0", roster, choices)
    assert bounds is not None
    for player in choices:
        assert bounds[player] >= season_value(sim, "team0", {"team0": (*roster, player)})
    expected = complete_add_choice(sim, roster)
    result = completion_candidates(
        sim, "team0", roster, choices, sim.as_of.astimezone(sim.zone).date(), True, None
    )
    assert min(result, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1])) == expected


@pytest.mark.parametrize("unsupported", ("transition", "injury", "error-category"))
def test_uncertified_future_rosters_and_error_categories_keep_complete_comparison(unsupported):
    sim = completion_case()
    if unsupported == "transition":
        sim.transitions["team0"] = ((sim.as_of.date() + timedelta(days=2), ("p0", "p6")),)
    elif unsupported == "injury":
        sim.snapshot = sim.snapshot.model_copy(
            update={
                "teams": tuple(
                    t.model_copy(update={"injury_players": {"p2": "IL"}}) if t.id == "team0" else t
                    for t in sim.snapshot.teams
                )
            }
        )
    else:
        ratio = fixture()[0].categories[6]
        sim.league = sim.league.model_copy(
            update={
                "categories": (
                    ratio.model_copy(
                        update={
                            "formula": ratio.formula.model_copy(
                                update={"zero_denominator": "error"}
                            )
                        }
                    ),
                )
            }
        )
    assert free_agent_bounds(sim, "team0", ("p0", "p1"), ("p6", "p7", "p8")) is None
    if unsupported == "error-category":
        with pytest.raises(DataError, match="zero denominator"):
            completion_candidates(
                sim, "team0", ("p0", "p1"), ("p6", "p7", "p8"), sim.as_of.date(), True, None
            )
    elif unsupported == "transition":
        expected = complete_add_choice(sim, ("p0", "p1"))
        result = completion_candidates(
            sim, "team0", ("p0", "p1"), ("p6", "p7", "p8"), sim.as_of.date(), True, None
        )
        assert [p for _, p in result] == ["p6", "p7", "p8"]
        assert (
            min(result, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1])) == expected
        )


@pytest.mark.parametrize("cancelled", (True, False))
def test_bounds_obey_cancellation_and_original_deadline(cancelled):
    sim = completion_case()
    if cancelled:
        sim.cancelled = lambda: True
    else:
        sim.deadline = monotonic() - 1, "trade_many"
    with pytest.raises(CalculationTimeout):
        free_agent_bounds(sim, "team0", ("p0", "p1"), ("p6", "p7", "p8"))


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("width", (1, 2, 64))
def test_bound_batches_preserve_every_scalar_ceiling_and_complete_winner(mode, width):
    sim = completion_case(mode, loose=True)
    sim.params = sim.params.model_copy(
        update={"lineup_batch": sim.params.lineup_batch.model_copy(update={"value": width})}
    )
    roster, choices = ("p0", "p1"), ("p6", "p7", "p8")
    reference = completion_case(mode, loose=True)
    reference.params = reference.params.model_copy(
        update={"lineup_batch": reference.params.lineup_batch.model_copy(update={"value": 1})}
    )
    assert free_agent_bounds(sim, "team0", roster, choices) == free_agent_bounds(
        reference, "team0", roster, choices
    )
    result = completion_candidates(sim, "team0", roster, choices, sim.as_of.date(), True, None)
    assert min(result, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1])) == (
        complete_add_choice(completion_case(mode, loose=True), roster)
    )
