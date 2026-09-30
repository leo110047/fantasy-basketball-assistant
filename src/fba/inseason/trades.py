from collections.abc import Callable
from itertools import combinations
from math import fsum
from typing import NamedTuple

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import InseasonPreferences, WeekForecast
from fba.contracts.inseason_results import TradePartner, TradeResult
from fba.formulas.registry import evaluate
from fba.inseason.matchup import Simulation, category_changes
from fba.inseason.recommendations import legal_roster
from fba.inseason.season import playoff_probability, season_forecasts, season_value


class IneligibleTrade(DataError):
    """A candidate is excluded because no legal resulting roster exists."""


def normalize_roster(
    sim: Simulation, team: str, roster: tuple[str, ...], available: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    target = len(sim.league.starter_slots) + sim.league.bench_slots
    added: list[str] = []
    dropped: list[str] = []
    on = sim.as_of.astimezone(sim.zone).date()
    while len(roster) > target:
        candidates = [
            (season_value(sim, team, {team: tuple(p for p in roster if p != drop)}), drop)
            for drop in roster
            if legal_roster(sim, tuple(p for p in roster if p != drop), on)
        ]
        if not candidates:
            raise IneligibleTrade(
                "trade.roster: cannot drop a player while preserving legal positions"
            )
        _, drop = min(
            candidates, key=lambda row: (-round(row[0] / sim.params.tolerance.value), row[1])
        )
        roster = tuple(p for p in roster if p != drop)
        dropped.append(drop)
    while len(roster) < target:
        candidates = [
            (season_value(sim, team, {team: (*roster, add)}), add)
            for add in available
            if add not in roster and legal_roster(sim, (*roster, add), on)
        ]
        if not candidates:
            raise IneligibleTrade("trade.roster: no eligible free agent can fill the open position")
        _, add = min(
            candidates, key=lambda row: (-round(row[0] / sim.params.tolerance.value), row[1])
        )
        roster = (*roster, add)
        added.append(add)
    if not legal_roster(sim, roster, on):
        raise IneligibleTrade("trade.roster: illegal positions after the trade")
    return tuple(sorted(roster)), tuple(added), tuple(dropped)


class TradeEffects(NamedTuple):
    rosters: dict[str, tuple[str, ...]]
    adds: dict[str, tuple[str, ...]]
    drops: dict[str, tuple[str, ...]]
    before: tuple[WeekForecast, ...]
    after: tuple[WeekForecast, ...]
    opponent_before: tuple[WeekForecast, ...]
    opponent_after: tuple[WeekForecast, ...]
    own_delta: FormulaTrace
    other_delta: FormulaTrace


def trade_effects(
    sim: Simulation,
    mine: str,
    opponent: str,
    send: tuple[str, ...],
    receive: tuple[str, ...],
) -> TradeEffects:
    if sim.league.trade_deadline is not None and sim.as_of >= sim.league.trade_deadline:
        raise DataError("trade_deadline: trading is closed")
    if (
        mine == opponent
        or not send
        or not receive
        or len(set(send)) != len(send)
        or len(set(receive)) != len(receive)
    ):
        raise DataError("trade: choose two distinct teams and nonempty, unique player lists")
    own, other = sim.roster(mine), sim.roster(opponent)
    if set(send) - set(own) or set(receive) - set(other):
        raise DataError("trade: one or more players are not on the selected rosters")
    available = tuple(f.player_id for f in sim.snapshot.free_agents if f.status == "free")
    rosters = {
        mine: tuple(p for p in own if p not in send) + receive,
        opponent: tuple(p for p in other if p not in receive) + send,
    }
    adds: dict[str, tuple[str, ...]] = {}
    drops: dict[str, tuple[str, ...]] = {}
    for team in sorted(rosters):
        rosters[team], adds[team], drops[team] = normalize_roster(
            sim, team, rosters[team], available
        )
        available = tuple(p for p in available if p not in adds[team])
    before = season_forecasts(sim, mine)
    after = season_forecasts(sim, mine, rosters)
    own_delta = evaluate(
        "difference", after=fsum(w.score for w in after), before=fsum(w.score for w in before)
    )
    opponent_before = season_forecasts(sim, opponent)
    opponent_after = season_forecasts(sim, opponent, rosters)
    other_delta = evaluate(
        "difference",
        after=fsum(w.score for w in opponent_after),
        before=fsum(w.score for w in opponent_before),
    )
    return TradeEffects(
        rosters, adds, drops, before, after, opponent_before, opponent_after, own_delta, other_delta
    )


def evaluate_trade(
    sim: Simulation,
    mine: str,
    opponent: str,
    send: tuple[str, ...],
    receive: tuple[str, ...],
    *,
    effects: TradeEffects | None = None,
) -> TradeResult:
    effects = effects if effects is not None else trade_effects(sim, mine, opponent, send, receive)
    rosters, adds, drops, before, after, opponent_before, opponent_after, own_delta, other_delta = (
        effects
    )
    own = sim.roster(mine)
    players = {
        p.player.id: p for p in sim.projection(sim.as_of.astimezone(sim.zone).date()).players
    }
    ranks: dict[str, float] = {}
    traces = [own_delta, other_delta]
    for pid in (*send, *receive):
        rank = players[pid].player.public_rank
        if rank is None:
            raise DataError(
                f"public_rank.{pid}: a public ranking source is required for acceptance estimates"
            )
        trace = evaluate(
            "rank_value",
            scale=sim.params.rank_scale.value,
            rank=float(rank),
            exponent=sim.params.rank_exponent.value,
        )
        ranks[pid] = trace.result
        traces.append(trace)
    rank_delta = evaluate(
        "difference", after=fsum(ranks[p] for p in send), before=fsum(ranks[p] for p in receive)
    )
    fitted = sim.acceptance_fit or {
        "beta_rank": sim.params.beta_rank.value,
        "beta_need": sim.params.beta_need.value,
        "threshold": sim.params.acceptance_threshold.value,
        "noise": sim.params.acceptance_noise.value,
    }
    accepted = evaluate(
        "acceptance",
        beta_rank=fitted["beta_rank"],
        beta_need=fitted["beta_need"],
        delta_rank=rank_delta.result,
        delta_need=other_delta.result,
        threshold=fitted["threshold"],
        noise=fitted["noise"],
    )
    gain = evaluate("product", gain=own_delta.result, probability=accepted.result)
    playoffs = evaluate(
        "difference",
        after=playoff_probability(sim, rosters)[mine],
        before=playoff_probability(sim)[mine],
    )
    playoff_dates = tuple(w for w in sim.league.matchups if w.id in sim.league.playoff_weeks)

    def playoff_games(roster: tuple[str, ...]) -> float:
        return float(
            sum(
                players[p].player.team_id in (g.home, g.away)
                and any(
                    w.start <= g.tipoff.astimezone(sim.zone).date() <= w.end for w in playoff_dates
                )
                for p in roster
                for g in sim.games
            )
        )

    games = evaluate("difference", after=playoff_games(rosters[mine]), before=playoff_games(own))
    return TradeResult(
        category_changes=category_changes(before, after),
        opponent_category_changes=category_changes(opponent_before, opponent_after),
        opponent=opponent,
        send=send,
        receive=receive,
        rosters=rosters,
        automatic_adds=adds,
        automatic_drops=drops,
        mine_delta=own_delta.result,
        opponent_delta=other_delta.result,
        playoff_delta=playoffs.result,
        playoff_games_delta=games.result,
        rank_delta=rank_delta.result,
        acceptance=accepted.result,
        expected_gain=gain.result,
        calibrated=sim.acceptance_fit is not None,
        before=before,
        after=after,
        opponent_before=opponent_before,
        opponent_after=opponent_after,
        traces=tuple((*traces, rank_delta, accepted, gain, playoffs, games)),
    )


def search_trades(
    sim: Simulation,
    preferences: InseasonPreferences,
    opponent: str | None,
    size: int,
    progress: Callable[[float], None],
) -> tuple[TradeResult, ...]:
    with sim.budget("trade_one" if size == 1 else "trade_many"):
        return search_trade_bundles(sim, preferences, opponent, size, progress)


def search_trade_bundles(
    sim: Simulation,
    preferences: InseasonPreferences,
    opponent: str | None,
    size: int,
    progress: Callable[[float], None],
) -> tuple[TradeResult, ...]:
    if not 1 <= size <= sim.params.max_trade_players.value:
        raise DataError("trade.size: outside configured search bounds")
    if size > 1 and opponent is None:
        raise DataError("trade.opponent: select a team for a multi-player search")
    mine = sim.snapshot.mine
    own = tuple(p for p in sim.roster(mine) if p not in preferences.untouchable)
    teams = tuple(
        t for t in sim.snapshot.teams if t.id != mine and (opponent is None or opponent == t.id)
    )
    candidates = [
        (t.id, send, receive)
        for t in teams
        for a in range(1, size + 1)
        for b in range(1, size + 1)
        for send in combinations(own, a)
        for receive in combinations(
            tuple(p for p in t.players if p not in preferences.ignored_opponents), b
        )
    ]
    result: list[TradeResult] = []
    for index, (team, send, receive) in enumerate(candidates):
        sim.check_limits()
        try:
            effects = trade_effects(sim, mine, team, send, receive)
            # These exact domain constraints can safely prune a completed bundle.
            # A losing smaller bundle cannot prune its supersets: category synergy
            # and unequal-roster replacement make that bound unsound.
            if size > 1 and (effects.own_delta.result <= 0 or effects.other_delta.result < 0):
                progress((index + 1) / len(candidates))
                continue
            trade = evaluate_trade(sim, mine, team, send, receive, effects=effects)
        except IneligibleTrade:
            progress((index + 1) / len(candidates))
            continue
        result.append(trade)
        progress((index + 1) / len(candidates))
    return tuple(
        sorted(
            result,
            key=lambda r: (
                -round(r.expected_gain / sim.params.tolerance.value),
                r.opponent,
                r.send,
                r.receive,
            ),
        )
    )


def complementary_teams(sim: Simulation) -> tuple[TradePartner, ...]:
    mine = sim.snapshot.mine
    forecasts = season_forecasts(sim, mine)
    own = {
        c.id: evaluate(
            "mean",
            values=tuple(
                next(x.probability for x in w.categories if x.id == c.id) for w in forecasts
            ),
        ).result
        for c in sim.league.categories
    }
    rows: list[TradePartner] = []
    for team in sim.snapshot.teams:
        if team.id == mine:
            continue
        other = season_forecasts(sim, team.id)
        vector = {
            c.id: evaluate(
                "mean",
                values=tuple(
                    next(x.probability for x in w.categories if x.id == c.id) for w in other
                ),
            ).result
            for c in sim.league.categories
        }
        contributions = tuple(
            evaluate("complementarity", home=(own[c],), away=(vector[c],)) for c in own
        )
        complementary = tuple(
            c for c, trace in zip(own, contributions, strict=True) if trace.result > 0
        )
        score = evaluate(
            "complementarity", home=tuple(own.values()), away=tuple(vector[c] for c in own)
        )
        rows.append(
            TradePartner(
                team_id=team.id,
                score=score.result,
                categories=complementary,
                traces=(score, *contributions),
            )
        )
    return tuple(
        sorted(rows, key=lambda r: (-round(r.score / sim.params.tolerance.value), r.team_id))
    )
