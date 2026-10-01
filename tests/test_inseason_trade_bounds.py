"""Trade ceilings must cover the independent complete effects engine."""

from datetime import timedelta
from time import monotonic

import numpy as np
import pytest
from inseason_support import fixture

from fba.contracts.config import Linear, Ratio, Term
from fba.contracts.inseason import CalculationTimeout
from fba.inseason.matchup import Simulation
from fba.inseason.roster_timeline import RosterChange
from fba.inseason.season import season_forecasts, season_value
from fba.inseason.trade_bounds import roster_ceiling
from fba.inseason.trades import trade_effects


def bound_case(mode, seed, coupled):
    args = list(fixture(mode=mode, teams=4))
    categories = list(args[0].categories)
    categories[0] = categories[0].model_copy(
        update={
            "formula": Linear(
                kind="linear",
                terms=(Term(stat_id="PTS", coefficient=2), Term(stat_id="TO", coefficient=-3)),
            ),
            "comparison_decimals": 12,
            "direction": "lower" if seed % 2 else "higher",
        }
    )
    categories[6] = categories[6].model_copy(
        update={
            "formula": Ratio(
                kind="ratio",
                numerator=(Term(stat_id="AST", coefficient=-1),),
                denominator=(Term(stat_id="TO", coefficient=1),),
                zero_denominator="numerator" if seed % 2 else "zero",
            ),
            "comparison_decimals": 12,
        }
    )
    args[0] = args[0].model_copy(update={"categories": tuple(categories)})
    if coupled:
        args[5] = args[5].model_copy(
            update={
                "pairings": tuple(
                    pair.model_copy(update={"away": "team2"})
                    if pair.home == "team0"
                    else pair.model_copy(update={"home": "team1"})
                    for pair in args[5].pairings
                )
            }
        )
    sim = Simulation(*args, samples=args[1].season_simulations.value)
    rng = np.random.default_rng(seed)
    for player in sim.players.players:
        for game in sim.games:
            if player.team_id in (game.home, game.away):
                sim.draws[player.id, game.id] = rng.uniform(-8, 20, (sim.samples, len(sim.axes)))
    return sim


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("coupled", (False, True))
@pytest.mark.parametrize("receive", (("p6",), ("p6", "p7")))
@pytest.mark.parametrize("seed", range(3))
def test_roster_ceiling_covers_complete_effects_and_changed_opponent(mode, coupled, receive, seed):
    sim = bound_case(mode, seed, coupled)
    before = season_forecasts(sim, "team0")
    other = season_forecasts(sim, "team2")
    upper = roster_ceiling(sim, before, other, ("p0",), receive)
    if coupled and len(receive) == 2:
        assert upper is None  # Opponent's automatic add has not been chosen.
        return
    reference = bound_case(mode, seed, coupled)
    effects = trade_effects(reference, "team0", "team2", ("p0",), receive, details=False)
    assert upper is not None
    assert upper >= season_value(reference, "team0", effects.rosters)


@pytest.mark.parametrize("unsupported", ("add", "transition", "injury", "error-category"))
def test_uncertified_roster_changes_keep_the_original_complete_search(unsupported):
    sim = bound_case("h2h_one_win", 0, False)
    own, other = season_forecasts(sim, "team0"), season_forecasts(sim, "team2")
    send = ("p0",)
    if unsupported == "add":
        send = ("p0", "p1")
    elif unsupported == "transition":
        sim.transitions["team0"] = (
            RosterChange(sim.as_of.date() + timedelta(days=2), "p12", "p1"),
        )
    elif unsupported == "injury":
        sim.snapshot = sim.snapshot.model_copy(
            update={
                "teams": tuple(
                    t.model_copy(update={"injury_players": {"p12": "IL"}}) if t.id == "team0" else t
                    for t in sim.snapshot.teams
                )
            }
        )
    else:
        ratio = sim.league.categories[6]
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
    assert roster_ceiling(sim, own, other, send, ("p6",)) is None


@pytest.mark.parametrize("cancelled", (True, False))
def test_roster_ceiling_obeys_cancellation_and_deadline(cancelled):
    sim = bound_case("h2h_one_win", 0, False)
    own, other = season_forecasts(sim, "team0"), season_forecasts(sim, "team2")
    if cancelled:
        sim.cancelled = lambda: True
    else:
        sim.deadline = monotonic() - 1, "trade_many"
    with pytest.raises(CalculationTimeout):
        roster_ceiling(sim, own, other, ("p0",), ("p6",))
