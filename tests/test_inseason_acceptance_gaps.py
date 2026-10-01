"""Independent finite-space/numeric oracles for the 2026-09-30 defects.

Synthetic evidence validates mechanics, never a real NBA calibration claim.
"""

import json
from datetime import timedelta
from itertools import product
from math import sqrt
from pathlib import Path

import numpy as np
import pytest
from inseason_support import fixture, simulation
from test_inseason_review import final_scores, prediction
from test_inseason_service import selected_session

from fba.contracts.base import DataError
from fba.contracts.config import DistributionParameters, StarterSlot
from fba.core.lineups import best_lineup
from fba.data.storage import Store
from fba.formulas.registry import evaluate
from fba.inseason.forecast_records import record_forecast
from fba.inseason.matchup import Simulation
from fba.inseason.review import calibration_bins, calibration_history, weekly_review
from fba.inseason.session import json_value


@pytest.mark.parametrize(
    "counts,minutes,model,rate,error",
    [
        ((2.0, 9.0), (1.0, 9.0), 1.0, 1.1, 0.331662479035540),
        ((0.0, 20.0), (10.0, 10.0), 1.0, 1.0, 1.0),
        ((0.0, 0.0), (10.0, 20.0), 0.2, 0.0, 0.081649658092773),
        ((1.0, 9.0), (1.0, 9.0), 1.0, 1.0, sqrt(0.1)),
    ],
)
def test_exposure_numeric_oracles(counts, minutes, model, rate, error):
    assert evaluate("exposure_rate", counts=counts, exposure=minutes).result == pytest.approx(rate)
    assert evaluate(
        "exposure_error", counts=counts, exposure=minutes, model=model
    ).result == pytest.approx(error)


@pytest.mark.parametrize("case", range(30))
def test_daily_batch_matches_independent_slot_assignment_enumeration(case):
    rng = np.random.default_rng(8000 + case)
    slots = tuple(
        StarterSlot(id=str(i), label=str(i), eligible_positions=(pos,))
        for i, pos in enumerate(("G", "F", "C"))
    )
    size = 6 + case % 10  # Explicitly reaches the <=15-player contract.
    positions = {
        f"p{i:02}": tuple(pos for pos in ("G", "F", "C") if rng.random() < 0.6) for i in range(size)
    }
    draws = {pid: rng.normal(size=(16, 3)) for pid in positions}
    away = rng.normal(size=(16, 3))

    def objective(ids):
        total = sum((draws[p] for p in ids), start=np.zeros_like(away))
        votes = np.sign(total - away).sum(axis=1)
        return float((votes > 0).mean())

    candidates = {}
    for row in product(
        *[(None, *(p for p in positions if s.eligible_positions[0] in positions[p])) for s in slots]
    ):
        chosen = tuple(sorted(p for p in row if p is not None))
        if len(set(chosen)) == len(chosen):
            candidates[chosen] = objective(chosen)
    expected = min(candidates, key=lambda ids: (-candidates[ids], -len(ids), ids))
    assignment, value = best_lineup(
        slots,
        positions,
        objective,
        1e-12,
        batch_objective=lambda rows: tuple(objective(row) for row in rows),
        batch_size=4,
    )
    assert tuple(sorted(assignment.values())) == expected
    assert value == candidates[expected]


def distribution():
    source = Path(__file__).with_name("fixtures") / "model-v6.json"
    return DistributionParameters.model_validate_json(
        json.dumps(
            {
                k: v
                for k, v in json.loads(source.read_bytes())["projection"].items()
                if k in DistributionParameters.model_fields
            }
        )
    )


def test_rookie_prior_is_stochastic_nested_and_uses_shared_sample_prefix():
    args = list(fixture())
    args[2] = args[2].model_copy(
        update={"boxes": tuple(b for b in args[2].boxes if b.player_id != "p0")}
    )
    args[3] = args[3].model_copy(update={"distribution": distribution()})
    full = Simulation(*args, samples=100_000)
    small = Simulation(*args, samples=200)
    game = next(g for g in full.games if g.home == "NBA0")
    a, b = full.game_draw("p0", game), small.game_draw("p0", game)
    assert np.array_equal(a[:200], b)
    assert np.var(a[:, full.axes.index("AST")]) > 0
    for child, parent in (("3PM", "FGM"), ("FGM", "FGA"), ("FTM", "FTA"), ("OREB", "REB")):
        assert np.all(a[:, full.axes.index(child)] <= a[:, full.axes.index(parent)])
    player = next(
        p
        for p in full.projection(game.tipoff.astimezone(full.zone).date()).players
        if p.player.id == "p0"
    )
    for stat in full.league.base_stats:
        target = player.rates.get(stat, 0.0) * player.minutes
        assert a[:, full.axes.index(stat)].mean() == pytest.approx(target, abs=0.065)
    assert np.array_equal(
        a[:, full.axes.index("PTS")],
        2 * a[:, full.axes.index("FGM")]
        + a[:, full.axes.index("3PM")]
        + a[:, full.axes.index("FTM")],
    )


def test_legacy_prior_without_nested_rules_fails_explicitly_for_rookie():
    args = list(fixture())
    args[2] = args[2].model_copy(update={"boxes": ()})
    sim = Simulation(*args)
    with pytest.raises(DataError, match="reload the preseason"):
        sim.game_draw("p0", next(g for g in sim.games if g.home == "NBA0"))


def test_week_and_category_calibration_have_independent_numeric_oracles():
    sim = simulation()
    assert sim.calibrated_score(0.75).result == pytest.approx(0.7)
    assert evaluate("calibration", p=0.75, c=sim.params.calibration.value).result == pytest.approx(
        0.705
    )
    assert sim.season().samples == sim.params.season_simulations.value == 40
    game = next(g for g in sim.games if g.home == "NBA0")
    assert np.array_equal(sim.game_draw("p0", game)[:40], sim.season().game_draw("p0", game))


@pytest.mark.parametrize("status,known_after", [("in_progress", False), ("completed", True)])
def test_started_or_new_completed_game_cannot_disappear_from_actual(status, known_after):
    args = list(fixture())
    now = args[-1]
    original = next(g for g in args[2].games if g.home == "NBA0")
    game = original.model_copy(
        update={
            "tipoff": now - timedelta(hours=1),
            "status": status,
            "known_at": now if known_after else now - timedelta(hours=2),
        }
    )
    args[2] = args[2].model_copy(update={"games": (game,)})
    if known_after:
        args[5] = args[5].model_copy(
            update={
                "actual": tuple(
                    a.model_copy(update={"through": now - timedelta(minutes=30)})
                    for a in args[5].actual
                )
            }
        )
    sim = Simulation(*args)
    with pytest.raises(DataError, match="has started|newer than"):
        sim.week("team0", "team1", "2")


def test_browsing_does_not_weight_review_or_discard_raw_history():
    sim = simulation()
    saved = prediction(sim)
    repeated = tuple(
        saved.model_copy(update={"id": str(i), "created_at": sim.as_of + timedelta(minutes=i)})
        for i in range(5)
    )
    report = weekly_review(sim.league, sim.params, final_scores(sim), repeated, "2", {})
    original = weekly_review(sim.league, sim.params, final_scores(sim), (repeated[0],), "2", {})
    assert report == original
    assert len(report.rows) == 9
    history = calibration_history("2026", repeated, (report,), ("a" * 64,))
    assert len(history.observations) == 9
    assert len(history.week_observations) == 1
    assert len(repeated) == 5


def test_sparse_and_sufficient_clustered_calibration_alerts():
    params = simulation().params
    sparse = calibration_bins(
        (0.9,) * 9, (0.0,) * 9, 10, clusters=("one-week",) * 9, params=params
    )[9]
    assert sparse.independent_samples == 1 and not sparse.alert
    enough = params.calibration_minimum.value
    dense = calibration_bins(
        (0.9,) * enough, (0.0,) * enough, 10, clusters=tuple(map(str, range(enough))), params=params
    )[9]
    assert dense.alert and dense.uncertainty > 0
    with pytest.raises(DataError, match="count differs"):
        calibration_bins((0.1,), (0.0,), 10, clusters=())


def test_unequal_week_weights_reduce_effective_calibration_samples():
    from math import sqrt

    params = simulation().params
    weights = (9, 1, 1, 1, 1, 1, 1, 1)
    clusters = tuple(str(i) for i, weight in enumerate(weights) for _ in range(weight))
    row = calibration_bins(
        (0.9,) * len(clusters), (0.0,) * len(clusters), 10, clusters=clusters, params=params
    )[9]
    effective = sum(weights) ** 2 / sum(w * w for w in weights)
    assert row.independent_samples == 8
    assert row.effective_samples == pytest.approx(effective)
    assert row.uncertainty == pytest.approx(
        params.calibration_confidence_z.value / (2 * sqrt(effective))
    )
    assert not row.alert


def test_content_origin_recording_is_idempotent_and_preserves_changed_decisions(
    tmp_path, monkeypatch
):
    session, now = prepared_session(tmp_path)
    original = session.simulation
    monkeypatch.setattr(
        session,
        "simulation",
        lambda **kwargs: original(**{"as_of": now, "require_league": False, **kwargs}),
    )
    for _ in range(5):
        record_forecast(session, "2")
    first = session.league_store().history("predictions")
    assert len(first) == 1
    session.params = session.params.model_copy(
        update={
            "week_calibration": session.params.week_calibration.model_copy(update={"value": 0.2})
        }
    )
    record_forecast(session, "2")
    assert len(session.league_store().history("predictions")) == 2
    assert session.league_store().history("predictions")[0] == first[0]


def test_bounded_history_does_not_load_or_delete_older_snapshots(tmp_path, monkeypatch):
    store = Store(tmp_path)
    at = simulation().as_of
    refs = [store.append_snapshot("history", "test", at, {"n": n}) for n in range(100)]
    calls = []
    original = store.load_snapshot

    def counted(sha):
        calls.append(sha)
        return original(sha)

    monkeypatch.setattr(store, "load_snapshot", counted)
    assert [s.payload["n"] for s in store.history("history", limit=3)] == [97, 98, 99]
    assert calls == refs[-3:]
    assert [s.payload["n"] for s in store.history("history", limit=2, before=refs[-3])] == [95, 96]
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 100


@pytest.mark.parametrize("case", range(30))
def test_joint_two_day_forecast_matches_independent_whole_week_enumeration(case):
    from fba.contracts.inseason import SeasonGame

    rng = np.random.default_rng(9000 + case)
    args = list(fixture())
    now = args[-1]
    args[0] = args[0].model_copy(
        update={
            "starter_slots": (StarterSlot(id="any", label="Any", eligible_positions=("PG", "C")),),
            "bench_slots": 2,
        }
    )
    games = tuple(
        SeasonGame(
            id=f"joint:{pid}:{day}",
            home=f"NBA{pid}",
            away="VISITOR",
            tipoff=now + timedelta(days=day, hours=12),
            known_at=now - timedelta(days=1),
            status="scheduled",
        )
        for pid in range(6)
        for day in range(2)
    )
    args[2] = args[2].model_copy(update={"games": games})
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(
                team.model_copy(update={"selected_slots": {"any": team.players[0]}})
                for team in args[5].teams
            )
        }
    )
    sim = Simulation(*args)
    for game in games:
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
    other, _ = sim.total("team1", "2")
    candidates = []
    for choices in product((None, "p0", "p1", "p2"), repeat=2):
        own = sum(
            (sim.draws[pid, f"joint:{pid[1:]}:{day}"] for day, pid in enumerate(choices) if pid),
            start=np.zeros_like(other),
        )
        votes = np.zeros(sim.samples)
        for stat in ("PTS", "REB", "AST", "STL", "BLK", "3PM"):
            votes += np.sign(own[:, sim.axes.index(stat)] - other[:, sim.axes.index(stat)])
        votes -= np.sign(own[:, sim.axes.index("TO")] - other[:, sim.axes.index("TO")])
        for made, attempted in (("FGM", "FGA"), ("FTM", "FTA")):

            def rate(total, made=made, attempted=attempted):
                num, den = (total[:, sim.axes.index(s)] for s in (made, attempted))
                return np.divide(num, den, out=np.zeros_like(num), where=den != 0).round(3)

            votes += np.sign(rate(own) - rate(other))
        candidates.append(float((votes > 0).mean()))
    forecast = sim.week("team0", "team1", "2")
    assert forecast.lineup_search == "joint_exact"
    assert forecast.raw_score == pytest.approx(max(candidates))


def prepared_session(tmp_path):
    session = selected_session(tmp_path)
    args = fixture()
    store = session.league_store()
    shas = [
        store.snapshot("test", args[-1], json_value(row.model_dump(mode="json")))
        for row in (args[2], args[3], args[5])
    ]
    state = session.state().model_copy(
        update=dict(
            zip(("players_sha256", "priors_sha256", "normalized_sha256"), shas, strict=True)
        )
    )
    session.save_state(state)
    return session, args[-1]


def test_session_cache_reuses_unchanged_inputs_and_invalidates_locks_and_parameters(tmp_path):
    session, now = prepared_session(tmp_path)
    a = session.simulation(as_of=now, require_league=False)
    game = next(g for g in a.games if g.home == "NBA0")
    first = a.game_draw("p0", game)
    a.week("team0", "team1", "2")
    b = session.simulation(as_of=now + timedelta(seconds=1), require_league=False)
    assert b.game_draw("p0", game) is first
    assert b.matchup_cache and b is not a
    c = session.simulation(as_of=game.tipoff, require_league=False)
    assert c.draws == {} and c.matchup_cache == {}
    session.params = session.params.model_copy(
        update={
            "week_calibration": session.params.week_calibration.model_copy(update={"value": 0.5})
        }
    )
    d = session.simulation(as_of=now, require_league=False)
    assert d.draws == {} and len(session.simulation_cache) <= 2


def test_previous_parameter_file_gets_only_missing_new_contract_fields(tmp_path):
    from inseason_support import DEFAULTS
    from test_inseason_data import Vault

    from fba.inseason.session import InseasonSession

    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    raw = session.params.model_dump(mode="json")
    raw["calibration"]["value"] = 0.33
    for key in (
        "week_calibration",
        "calibration_minimum",
        "calibration_confidence_z",
        "weekly_exact_candidates",
        "lineup_batch",
        "scenario_cache_entries",
        "prior_predictive",
    ):
        raw.pop(key)
    (tmp_path / "parameters.json").write_text(json.dumps(raw))
    restarted = InseasonSession(tmp_path, DEFAULTS, Vault())
    assert restarted.params.calibration.value == 0.33
    assert restarted.params.week_calibration.value == 0.8
    assert restarted.params.prior_predictive.value == "nested_poisson"


def test_partial_availability_minute_redistribution_preserves_expected_team_budget():
    from fba.inseason.adjustments import redistribute

    sim = simulation()
    projection = sim.projection(sim.as_of.date())
    group = projection.players[:3]
    modified = tuple(
        p.model_copy(
            update={"player": p.player.model_copy(update={"team_id": "same"}), "probability": q}
        )
        for p, q in zip(group, (0.5, 1.0, 0.25), strict=True)
    )
    projection = projection.model_copy(update={"players": modified})
    result = redistribute(projection, "p0", 35.0)
    before = sum(p.minutes * p.probability for p in modified)
    after = sum(result[p.player.id] * p.probability for p in modified)
    assert before == pytest.approx(after)
    assert result["p0"] == 35.0


def test_il_return_is_not_zeroed_and_activation_has_capacity_and_explicit_drop():
    from fba.contracts.config import InjurySlot

    args = list(fixture())
    args[0] = args[0].model_copy(
        update={
            "injury_slots": (InjurySlot(id="IL", label="IL", count=1, eligible_statuses=("INJ",)),)
        }
    )
    players = tuple(
        p.model_copy(update={"status": "INJ", "return_on": args[-1].date() + timedelta(days=2)})
        if p.id == "p6"
        else p
        for p in args[2].players
    )
    args[2] = args[2].model_copy(update={"players": players})
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL"}}) if t.id == "team0" else t
                for t in args[5].teams
            ),
            "free_agents": tuple(f for f in args[5].free_agents if f.player_id != "p6"),
        }
    )
    sim = Simulation(*args)
    forecast = sim.season().week("team0", "team1", "3")
    assert len(forecast.injury_returns) == 1
    move = forecast.injury_returns[0]
    assert move.player_id == "p6" and move.drop in ("p0", "p1", "p2") and move.estimated
    assert all(
        len(day.slots) + len(day.bench) <= 3 for day in forecast.lineups if day.team_id == "team0"
    )
    assert any("p6" in day.slots.values() for day in forecast.lineups if day.team_id == "team0")


def test_missing_public_rank_keeps_trade_forecast_but_not_fabricated_acceptance():
    from fba.inseason.trades import evaluate_trade

    sim = simulation()
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"public_rank": None}) if p.id == "p0" else p
                for p in sim.players.players
            )
        }
    )
    result = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    assert result.before and result.after
    assert result.acceptance is None and result.expected_gain is None and result.rank_delta is None
    assert "p0" in result.acceptance_unavailable


def test_cache_matches_cold_projection_across_strict_played_at_boundary(tmp_path):
    session, now = prepared_session(tmp_path)
    sim = session.simulation(as_of=now, require_league=False)
    box = sim.players.boxes[0].model_copy(
        update={"game_id": "new-box", "known_at": now, "played_at": now, "minutes": 100.0}
    )
    players = sim.players.model_copy(update={"boxes": (*sim.players.boxes, box)})
    store = session.league_store()
    sha = store.snapshot("test", now, json_value(players.model_dump(mode="json")))
    session.save_state(session.state().model_copy(update={"players_sha256": sha}))
    initial = session.simulation(as_of=now, require_league=False).projection(now.date())
    cached = session.simulation(as_of=now + timedelta(seconds=1), require_league=False)
    actual = cached.projection(now.date())
    session.simulation_cache.clear()
    cold = session.simulation(as_of=now + timedelta(seconds=1), require_league=False)
    assert actual == cold.projection(now.date())
    assert actual.players[0].minutes != initial.players[0].minutes


def test_completed_game_requires_coverage_separate_from_retrieval_time():
    args = list(fixture())
    now = args[-1]
    game = (
        args[2]
        .games[0]
        .model_copy(
            update={
                "tipoff": now - timedelta(hours=2),
                "known_at": now - timedelta(hours=1),
                "status": "completed",
            }
        )
    )
    args[2] = args[2].model_copy(update={"games": (game,)})
    with pytest.raises(DataError, match="no verified Yahoo score coverage"):
        Simulation(*args).actual("team0", "2")
    args[5] = args[5].model_copy(
        update={
            "actual": tuple(a.model_copy(update={"complete_through": now}) for a in args[5].actual)
        }
    )
    confirmed, _ = Simulation(*args).actual("team0", "2")
    assert np.all(confirmed == 0)  # Legitimate zero is allowed only with coverage evidence.


def test_healthy_il_return_actions_and_bench_slot_agree():
    from fba.contracts.config import InjurySlot
    from fba.inseason.today import today

    args = list(fixture())
    args[0] = args[0].model_copy(
        update={
            "injury_slots": (InjurySlot(id="IL", label="IL", count=1, eligible_statuses=("INJ",)),)
        }
    )
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL"}}) if t.id == "team0" else t
                for t in args[5].teams
            ),
            "free_agents": tuple(f for f in args[5].free_agents if f.player_id != "p6"),
        }
    )
    sim = Simulation(*args)
    sim.week("team0", "team1", "2")  # The existing IL return plan is available to Today.
    result = today(sim, sim.as_of.date(), "Asia/Taipei")
    row = next(p for p in result.players if p.player_id == "p6")
    action = next(a for a in result.actions if a.player_id == "p6" and a.kind in ("start", "bench"))
    assert row.slot == action.slot
    assert row.slot != "IL"
    dropped = next(a for a in result.actions if a.kind == "drop")
    activation = next(a for a in result.actions if a.kind == "injury_out")
    assert dropped.player_id in activation.reason


def test_trade_bound_does_not_read_future_rank_versions():
    from fba.inseason.season import season_forecasts
    from fba.inseason.trade_bounds import gain_bound

    sim = simulation(teams=4)
    own, other = (season_forecasts(sim, t) for t in ("team0", "team1"))
    expected = gain_bound(sim, own, other, ("p0",), ("p3",))
    args = list(fixture(teams=4))
    future = (
        args[2]
        .players[0]
        .model_copy(update={"known_at": args[-1] + timedelta(days=1), "public_rank": 100000})
    )
    args[2] = args[2].model_copy(update={"players": (*args[2].players, future)})
    assert gain_bound(Simulation(*args), own, other, ("p0",), ("p3",)) == expected


def test_prediction_identity_lookup_migrates_once_and_preserves_all_history(tmp_path, monkeypatch):
    store = Store(tmp_path)
    now = fixture()[-1]
    for i in range(100):
        store.append_snapshot("predictions", "test", now, {"id": str(i)})
    calls = []
    original = store.load_snapshot

    def load(sha):
        calls.append(sha)
        return original(sha)

    monkeypatch.setattr(store, "load_snapshot", load)
    assert store.find_snapshot("predictions", "42").payload == {"id": "42"}
    assert len(calls) == 101
    calls.clear()
    assert store.find_snapshot("predictions", "42").payload == {"id": "42"}
    assert len(calls) == 1
    assert len(tuple((tmp_path / "snapshots").glob("*.json"))) == 100
