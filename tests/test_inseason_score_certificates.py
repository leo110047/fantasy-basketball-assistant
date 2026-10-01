"""Point-only certificates must agree with the real complete lineup engine."""

from time import monotonic

import numpy as np
import pytest
from inseason_support import simulation

from fba.contracts.inseason import CalculationTimeout
from fba.inseason.forecast import cached_matchup_points, forecast_key, forecast_score
from fba.inseason.lineup_bounds import certified_loss


def score_case(mode="h2h_one_win", week_tie=0.5, *, tied=False):
    sim = simulation(mode=mode).season()
    sim.league = sim.league.model_copy(
        update={"categories": (sim.league.categories[1],), "week_tie_value": week_tie}
    )
    if tied:
        sim.games = ()
        sim.snapshot = sim.snapshot.model_copy(
            update={
                "actual": tuple(
                    row.model_copy(update={"totals": {key: 0.0 for key in row.totals}})
                    for row in sim.snapshot.actual
                )
            }
        )
    for player in sim.players.players:
        for game in sim.games:
            if player.team_id in (game.home, game.away):
                box = np.zeros((sim.samples, len(sim.axes)))
                box[:, sim.axes.index("REB")] = 1 if player.id in sim.roster("team0") else 1000
                sim.draws[player.id, game.id] = box
    return sim


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("week_tie", (0.0, 0.5, 1.0))
def test_points_scores_and_later_complete_details_match_independent_full_engine(mode, week_tie):
    sim, reference = score_case(mode, week_tie), score_case(mode, week_tie)
    a, b, _ = reference.matchup_totals("team0", "team1", "3")
    expected = reference.score(a, b, standings=True)[1], reference.score(b, a, standings=True)[1]
    actual = cached_matchup_points(sim, "team0", "team1", "3")
    for got, wanted in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(got, wanted)
    assert (
        forecast_score(sim, "team0", "team1", "3")
        == reference.calibrated_score(float(reference.score(a, b)[1].mean())).result
    )
    if mode == "h2h_one_win":
        assert forecast_key(sim, "team0", "team1", "3") not in sim.matchup_cache
    assert sim.week("team0", "team1", "3") == reference.week("team0", "team1", "3")


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("week_tie", (0.0, 0.5, 1.0))
def test_a_possible_tie_is_not_certified_as_a_loss_or_counted_as_a_strict_win(mode, week_tie):
    sim = score_case(mode, week_tie, tied=True)
    assert not certified_loss(sim, "team0", "team1", "3")
    points = cached_matchup_points(sim, "team0", "team1", "3")
    expected = week_tie if mode == "h2h_one_win" else sim.league.categories[0].tie_value
    np.testing.assert_array_equal(points, np.full((2, sim.samples), expected))
    raw = 0.0 if mode == "h2h_one_win" else expected
    assert forecast_score(sim, "team0", "team1", "3") == sim.calibrated_score(raw).result


def test_changed_opponent_rosters_and_lru_eviction_cannot_reuse_old_point_draws():
    sim = score_case()
    original = cached_matchup_points(sim, "team0", "team1", "3")
    changed = {"team0": sim.roster("team1"), "team1": sim.roster("team0")}
    reference = score_case()
    a, b, _ = reference.matchup_totals("team0", "team1", "3", changed)
    sim.params = sim.params.model_copy(
        update={
            "scenario_cache_entries": sim.params.scenario_cache_entries.model_copy(
                update={"value": 1}
            )
        }
    )
    actual = cached_matchup_points(sim, "team0", "team1", "3", changed)
    np.testing.assert_array_equal(actual[0], reference.score(a, b, standings=True)[1])
    assert not np.array_equal(actual[0], original[0])
    assert len(sim.standings_point_cache) == 1
    np.testing.assert_array_equal(cached_matchup_points(sim, "team0", "team1", "3"), original)


def test_tiny_calibration_cannot_turn_a_real_win_into_a_loss_certificate():
    sim, reference = score_case(), score_case()
    for candidate in (sim, reference):
        candidate.params = candidate.params.model_copy(
            update={
                "week_calibration": candidate.params.week_calibration.model_copy(
                    update={"value": 1e-20}
                )
            }
        )
    changed = {"team0": sim.roster("team1"), "team1": sim.roster("team0")}
    a, b, _ = reference.matchup_totals("team0", "team1", "3", changed)
    expected = reference.score(a, b, standings=True)[1]
    np.testing.assert_array_equal(expected, np.ones(sim.samples))
    assert not certified_loss(sim, "team0", "team1", "3", changed)
    np.testing.assert_array_equal(
        cached_matchup_points(sim, "team0", "team1", "3", changed)[0], expected
    )


@pytest.mark.parametrize("cancelled", (True, False))
def test_warm_point_draws_still_obey_cancellation_and_original_deadline(cancelled):
    sim = score_case()
    cached_matchup_points(sim, "team0", "team1", "3")
    if cancelled:
        sim.cancelled = lambda: True
    else:
        sim.deadline = monotonic() - 1, "trade_many"
    with pytest.raises(CalculationTimeout):
        cached_matchup_points(sim, "team0", "team1", "3")
