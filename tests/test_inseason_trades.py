from itertools import combinations, product

from inseason_support import DEFAULTS, simulation

from fba.contracts.inseason import InseasonPreferences
from fba.inseason.trades import IneligibleTrade, evaluate_trade, search_trades


def permitted_value(sim, prefs, send, receive):
    ranks = {
        p.player.id: p.player.public_rank
        for p in sim.projection(sim.as_of.astimezone(sim.zone).date()).players
    }
    if any(ranks[p] is None for p in (*send, *receive)):
        return False
    own = sum(
        sim.params.rank_scale.value / ranks[p] ** sim.params.rank_exponent.value for p in send
    )
    other = sum(
        sim.params.rank_scale.value / ranks[p] ** sim.params.rank_exponent.value for p in receive
    )
    return (
        min(own, other) / max(own, other) + sim.params.tolerance.value
        >= prefs.trade_value_min_ratio
    )


def test_pruned_two_by_two_top_ten_match_complete_small_league_search(monkeypatch):
    sim = simulation(teams=4)
    # Similar public values keep enough legal, mutually beneficial bundles
    # inside the widest permitted market range for a complete top ten.
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"public_rank": p.public_rank + 2}) for p in sim.players.players
            )
        }
    )
    sim.params = sim.params.model_copy(
        update={"beta_rank": sim.params.beta_rank.model_copy(update={"value": 1.0})}
    )
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
    prefs = prefs.model_copy(
        update={"untouchable": (), "ignored_opponents": (), "trade_value_min_ratio": 0.5}
    )
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
            if (
                row.mine_delta > 0
                and row.opponent_delta >= 0
                and permitted_value(sim, prefs, send, receive)
            ):
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
    # The contract is exact top-ten parity, not evaluating every lower-ranked
    # positive bundle. A sound upper bound must skip actual trade_effects work.
    assert len(calls) <= len(expected) < len(all_results)
    counts = sim.trade_search_counts
    assert counts["bounded"] > 0
    assert counts["full_effects"] < counts["candidates"]
    assert counts["full_effects"] + counts["bounded"] == counts["eligible"]
    assert (
        counts["eligible"] + counts["value_filtered"] + counts["unknown_value"]
        == counts["candidates"]
    )


def test_one_by_one_search_evaluates_all_eligible_teams_and_respects_exclusions():
    sim = simulation(teams=4)
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"public_rank": 2 + i % 3})
                for i, p in enumerate(sim.players.players)
            )
        }
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"untouchable": ("p0",), "ignored_opponents": ("p3",)})
    found = search_trades(sim, prefs, None, 1, lambda _: None)
    expected = []
    for team in sim.snapshot.teams[1:]:
        for send, receive in product(sim.roster("team0"), team.players):
            if send == "p0" or receive == "p3":
                continue
            if not permitted_value(sim, prefs, (send,), (receive,)):
                continue
            try:
                expected.append(evaluate_trade(sim, "team0", team.id, (send,), (receive,)))
            except IneligibleTrade:
                continue
    assert {(r.opponent, r.send, r.receive) for r in found} == {
        (r.opponent, r.send, r.receive) for r in expected
    }
    assert {r.opponent for r in found} == {"team1", "team2", "team3"}
