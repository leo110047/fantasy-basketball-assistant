"""Market screening is distinct from acceptance and manual trade evaluation."""

import json
from itertools import combinations, product

import pytest
from inseason_support import DEFAULTS, simulation
from test_inseason_data import Vault

from fba.apps.inseason.api import basic_action
from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonPreferences
from fba.inseason.operations import bootstrap
from fba.inseason.session import InseasonSession
from fba.inseason.trades import evaluate_trade, search_trades, trade_effects, trade_summary


def preferences(**values):
    data = json.loads((DEFAULTS / "preferences.json").read_text())
    return InseasonPreferences.model_validate_json(json.dumps({**data, **values}))


def ranked_simulation(ranks):
    sim = simulation(teams=4)
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"public_rank": ranks.get(p.id, 100)})
                for p in sim.players.players
            )
        }
    )
    return sim


def test_legacy_preferences_default_to_seventy_percent_and_publish_same_bounds(tmp_path):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    payload = session.preferences.model_dump(mode="json")
    payload.pop("trade_value_min_ratio")
    (tmp_path / "preferences.json").write_text(json.dumps(payload))
    loaded = InseasonSession(tmp_path, DEFAULTS, Vault())
    assert loaded.preferences.trade_value_min_ratio == 0.7
    control = bootstrap(loaded)["preference_controls"]["trade_value_min_ratio"]
    assert (control["minimum"], control["default"], control["maximum"]) == (0.5, 0.7, 1.0)


@pytest.mark.parametrize("ratio", (0.5, 0.7, 1.0))
def test_trade_range_is_saved_and_survives_restart(tmp_path, ratio):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    payload = session.preferences.model_dump(mode="json")
    basic_action(
        session, "preferences", json.dumps({**payload, "trade_value_min_ratio": ratio}).encode()
    )
    assert session.preferences.trade_value_min_ratio == ratio
    assert InseasonSession(tmp_path, DEFAULTS, Vault()).preferences.trade_value_min_ratio == ratio


@pytest.mark.parametrize("ratio", (0.49, 1.01, None, "nan"))
def test_invalid_trade_range_fails_without_overwriting_preferences(tmp_path, ratio):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    original = (tmp_path / "preferences.json").read_bytes()
    payload = session.preferences.model_dump(mode="json")
    with pytest.raises(DataError, match="trade_value_min_ratio"):
        basic_action(
            session,
            "preferences",
            json.dumps({**payload, "trade_value_min_ratio": ratio}).encode(),
        )
    assert (tmp_path / "preferences.json").read_bytes() == original
    assert session.preferences.trade_value_min_ratio == 0.7


@pytest.mark.parametrize(
    "minimum,eligible", ((1.0, {"p3"}), (0.7, {"p3", "p4"}), (0.5, {"p3", "p4", "p5"}))
)
def test_widening_range_includes_only_fair_candidates_before_simulation(
    monkeypatch, minimum, eligible
):
    sim = ranked_simulation({"p0": 100, "p3": 100, "p4": 125, "p5": 200})
    prefs = preferences(untouchable=["p1", "p2"], trade_value_min_ratio=minimum)
    original = evaluate_trade
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(args[4][0])
        return original(*args, **kwargs)

    monkeypatch.setattr("fba.inseason.trades.evaluate_trade", evaluate)
    rows = search_trades(sim, prefs, "team1", 1, lambda _: None)
    assert set(calls) == eligible
    assert {p for row in rows for p in row.receive} == eligible
    assert sim.trade_search_counts["value_filtered"] == 3 - len(eligible)


def test_superstar_for_bench_is_rejected_without_ros_and_manual_evaluation_remains_available(
    monkeypatch,
):
    sim = ranked_simulation({"p0": 1, "p3": 150, "p4": 151, "p5": 152})
    prefs = preferences(untouchable=["p1", "p2"])

    def unexpected(*args, **kwargs):
        pytest.fail("Unfair bundles must be screened before season simulation")

    monkeypatch.setattr("fba.inseason.trades.season_forecasts", unexpected)
    assert search_trades(sim, prefs, "team1", 1, lambda _: None) == ()
    assert sim.trade_search_counts["value_filtered"] == 3
    monkeypatch.undo()
    manual = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    assert manual.rank_delta is not None and manual.rank_delta > 90


def test_missing_rank_is_explicit_and_never_fabricated_for_search():
    sim = ranked_simulation({"p0": 100, "p3": None, "p4": 100, "p5": 100})
    rows = search_trades(sim, preferences(untouchable=["p1", "p2"]), "team1", 1, lambda _: None)
    assert sim.trade_search_counts["unknown_value"] == 1
    assert {p for row in rows for p in row.receive} == {"p4", "p5"}
    manual = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    assert manual.acceptance is None and manual.expected_gain is None
    assert "p3" in manual.acceptance_unavailable


def test_market_filter_compares_entire_bundle_and_reports_complete_accounting():
    sim = ranked_simulation({"p0": 100, "p1": 101, "p2": 102, "p6": 200, "p7": 201, "p8": 202})
    prefs = preferences(untouchable=[], trade_value_min_ratio=0.7)
    evaluated = []

    def progress(_):
        evaluated.append(True)

    search_trades(sim, prefs, "team2", 2, progress)
    raw = [
        (send, receive)
        for a, b in product((1, 2), repeat=2)
        for send, receive in product(
            combinations(sim.roster("team0"), a), combinations(sim.roster("team2"), b)
        )
    ]
    ranks = {p.id: p.public_rank for p in sim.players.players}
    allowed = [
        (a, b)
        for a, b in raw
        if min(sum(1 / ranks[p] for p in a), sum(1 / ranks[p] for p in b))
        / max(sum(1 / ranks[p] for p in a), sum(1 / ranks[p] for p in b))
        >= 0.7
    ]
    assert allowed and all(len(a) == 1 and len(b) == 2 for a, b in allowed)
    counts = sim.trade_search_counts
    assert counts["eligible"] == len(allowed)
    assert counts["candidates"] == len(raw)
    assert (
        counts["candidates"]
        == counts["eligible"] + counts["value_filtered"] + counts["unknown_value"]
    )
    assert counts["eligible"] == counts["full_effects"] + counts["bounded"] == len(evaluated)


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize(
    "send,receive", ((("p0",), ("p3", "p4", "p5")), (("p0", "p1", "p2"), ("p3",)))
)
def test_three_player_unequal_exchange_completes_both_vacancies_or_releases(mode, send, receive):
    sim = simulation(mode=mode)
    result = evaluate_trade(sim, "team0", "team1", send, receive)
    assert all(len(roster) == 3 and len(set(roster)) == 3 for roster in result.rosters.values())
    assert sum(map(len, result.automatic_adds.values())) == 2
    assert sum(map(len, result.automatic_drops.values())) == 2
    assert not set(result.rosters["team0"]).intersection(result.rosters["team1"])
    assert evaluate_trade(sim, "team0", "team1", send, receive) == result


def test_automatic_unequal_trade_drops_respect_protected_own_players():
    sim = simulation()
    sim.untouchable = frozenset(("p1", "p2"))
    result = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3", "p4", "p5"))
    assert {"p1", "p2"}.issubset(result.rosters["team0"])
    assert len(result.automatic_drops["team0"]) == 2
    assert not set(result.automatic_drops["team0"]).intersection(sim.untouchable)


def test_score_only_effects_cannot_be_published_as_complete_details():
    sim = simulation()
    effects = trade_effects(sim, "team0", "team1", ("p0",), ("p3",), details=False)
    summary, _ = trade_summary(sim, "team0", "team1", ("p0",), ("p3",), effects=effects)
    complete = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    assert summary.mine_delta == complete.mine_delta
    assert summary.opponent_delta == complete.opponent_delta
    with pytest.raises(DataError, match="complete forecasts"):
        evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",), effects=effects)
