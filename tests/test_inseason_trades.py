from itertools import combinations, product

from inseason_support import DEFAULTS, simulation

from fba.contracts.inseason import InseasonPreferences
from fba.inseason.trades import IneligibleTrade, evaluate_trade, search_trades


def test_pruned_two_by_two_top_ten_match_complete_small_league_search(monkeypatch):
    sim = simulation(teams=4)
    sim.league = sim.league.model_copy(update={"bench_slots": 2})
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"players": (*t.players, "p12" if t.id == "team0" else "p13")})
                if t.id in ("team0", "team2")
                else t
                for t in sim.snapshot.teams
            ),
            "free_agents": tuple(
                f for f in sim.snapshot.free_agents if f.player_id not in ("p12", "p13")
            ),
        }
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"untouchable": (), "ignored_opponents": ()})
    oracle = []
    all_results = []
    for a, b in product((1, 2), repeat=2):
        for send, receive in product(
            combinations(sim.roster("team0"), a), combinations(sim.roster("team2"), b)
        ):
            try:
                row = evaluate_trade(sim, "team0", "team2", send, receive)
            except IneligibleTrade:
                continue
            all_results.append(row)
            if row.mine_delta > 0 and row.opponent_delta >= 0:
                oracle.append(row)
    expected = sorted(
        oracle,
        key=lambda r: (
            -round(r.expected_gain / sim.params.tolerance.value),
            r.opponent,
            r.send,
            r.receive,
        ),
    )
    assert len(expected) >= 10, "Fixture must exercise a complete top ten"
    calls = []
    original = evaluate_trade

    def record(*args, **kwargs):
        calls.append((args[3], args[4]))
        return original(*args, **kwargs)

    monkeypatch.setattr("fba.inseason.trades.evaluate_trade", record)
    found = search_trades(sim, prefs, "team2", 2, lambda _: None)
    assert found[:10] == tuple(expected[:10])
    assert len(calls) == len(expected) < len(all_results)


def test_one_by_one_search_evaluates_all_eligible_teams_and_respects_exclusions():
    sim = simulation(teams=4)
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"untouchable": ("p0",), "ignored_opponents": ("p3",)})
    found = search_trades(sim, prefs, None, 1, lambda _: None)
    expected = []
    for team in sim.snapshot.teams[1:]:
        for send, receive in product(sim.roster("team0"), team.players):
            if send == "p0" or receive == "p3":
                continue
            try:
                expected.append(evaluate_trade(sim, "team0", team.id, (send,), (receive,)))
            except IneligibleTrade:
                continue
    assert {(r.opponent, r.send, r.receive) for r in found} == {
        (r.opponent, r.send, r.receive) for r in expected
    }
    assert {r.opponent for r in found} == {"team1", "team2", "team3"}
