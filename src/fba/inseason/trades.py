from collections.abc import Callable, Iterable
from datetime import date
from itertools import combinations
from math import fsum
from typing import Literal, NamedTuple, overload

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import InseasonPreferences, WeekForecast
from fba.contracts.inseason_results import TradePartner, TradeResult, TradeSummary
from fba.formulas.registry import evaluate
from fba.inseason.forecast import category_changes
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import legal_roster
from fba.inseason.season import playoff_probability, season_forecasts, season_value
from fba.inseason.trade_bounds import free_agent_bounds, gain_bound, season_limits

type TradeCandidate = tuple[str, tuple[str, ...], tuple[str, ...]]


class IneligibleTrade(DataError):
    """A candidate is excluded because no legal resulting roster exists."""


def normalize_roster(
    sim: Simulation, team: str, roster: tuple[str, ...], available: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    target = len(sim.league.starter_slots) + sim.league.bench_slots
    added: list[str] = []
    dropped: list[str] = []
    on = sim.as_of.astimezone(sim.zone).date()
    known = {p.player.id for p in sim.projection(on).players}
    if len(set(roster)) != len(roster) or set(roster) - known:
        raise IneligibleTrade("trade.roster: duplicate or unknown player")
    maximum = season_limits(sim, season_forecasts(sim, team))[1] if len(roster) != target else None
    while len(roster) > target:
        candidates = completion_candidates(sim, team, roster, roster, on, False, maximum)
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
        candidates = completion_candidates(sim, team, roster, available, on, True, maximum)
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


def completion_candidates(
    sim: Simulation,
    team: str,
    roster: tuple[str, ...],
    choices: tuple[str, ...],
    on: date,
    adding: bool,
    maximum: float | None,
) -> list[tuple[float, str]]:
    if adding:
        return addition_candidates(sim, team, roster, choices, on, maximum)
    result: list[tuple[float, str]] = []
    for player in sorted(choices):
        if team == sim.snapshot.mine and player in sim.untouchable:
            continue
        changed = tuple(p for p in roster if p != player)
        capacity = len(sim.league.starter_slots) + sim.league.bench_slots
        # A 1-for-3 exchange needs two sequential releases. Its first
        # internal comparison can still exceed capacity; the final roster
        # must pass the ordinary capacity/identity validation.
        if len(changed) <= capacity and not legal_roster(sim, changed, on):
            continue
        value = season_value(sim, team, {team: changed})
        result.append((value, player))
        # Lexical order preserves the complete search's tie breaker. Once
        # this choice reaches the mathematical ceiling, none can rank higher.
        if maximum is not None and round(value / sim.params.tolerance.value) >= round(
            maximum / sim.params.tolerance.value
        ):
            break
    return result


def addition_candidates(
    sim: Simulation,
    team: str,
    roster: tuple[str, ...],
    choices: tuple[str, ...],
    on: date,
    maximum: float | None,
) -> list[tuple[float, str]]:
    available = tuple(
        p for p in sorted(choices) if p not in roster and legal_roster(sim, (*roster, p), on)
    )
    if not available:
        return []
    tolerance = sim.params.tolerance.value
    first, *remaining = available
    value = season_value(sim, team, {team: (*roster, first)})
    result = [(value, first)]
    best = -round(value / tolerance), first
    if maximum is not None and round(value / tolerance) >= round(maximum / tolerance):
        return result  # The original lexical-first global-ceiling shortcut.
    bounds = free_agent_bounds(sim, team, roster, tuple(remaining))
    if bounds is not None:
        remaining.sort(key=lambda p: (-bounds[p], p))
    for player in remaining:
        sim.check_limits()
        if bounds is not None and (-round(bounds[player] / tolerance), player) >= best:
            continue  # Even its relaxed ceiling cannot beat the incumbent's exact rank.
        value = season_value(sim, team, {team: (*roster, player)})
        result.append((value, player))
        best = min(best, (-round(value / tolerance), player))
        if (
            bounds is None
            and maximum is not None
            and round(value / tolerance) >= round(maximum / tolerance)
        ):
            break
        # With reordered bounds, earlier IDs can still tie a ceiling winner.
        # Their bounds must be checked instead of stopping at that winner.
    return result


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
    details: bool = True


def trade_effects(
    sim: Simulation,
    mine: str,
    opponent: str,
    send: tuple[str, ...],
    receive: tuple[str, ...],
    *,
    details: bool = True,
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
    before = season_forecasts(sim, mine) if details else ()
    after = season_forecasts(sim, mine, rosters) if details else ()
    own_delta = evaluate(
        "difference",
        after=fsum(w.score for w in after) if details else season_value(sim, mine, rosters),
        before=fsum(w.score for w in before) if details else season_value(sim, mine),
    )
    opponent_before = season_forecasts(sim, opponent) if details else ()
    opponent_after = season_forecasts(sim, opponent, rosters) if details else ()
    other_delta = evaluate(
        "difference",
        after=fsum(w.score for w in opponent_after)
        if details
        else season_value(sim, opponent, rosters),
        before=fsum(w.score for w in opponent_before) if details else season_value(sim, opponent),
    )
    return TradeEffects(
        rosters,
        adds,
        drops,
        before,
        after,
        opponent_before,
        opponent_after,
        own_delta,
        other_delta,
        details,
    )


def trade_summary(
    sim: Simulation,
    mine: str,
    opponent: str,
    send: tuple[str, ...],
    receive: tuple[str, ...],
    *,
    effects: TradeEffects,
) -> tuple[TradeSummary, tuple[FormulaTrace, ...]]:
    rosters, adds, drops = effects.rosters, effects.adds, effects.drops
    own_delta, other_delta = effects.own_delta, effects.other_delta
    own = sim.roster(mine)
    players = {
        p.player.id: p for p in sim.projection(sim.as_of.astimezone(sim.zone).date()).players
    }
    ranks: dict[str, float] = {}
    traces = [own_delta, other_delta]
    missing_ranks = tuple(
        pid for pid in (*send, *receive) if players[pid].player.public_rank is None
    )
    rank_delta = accepted = gain = None
    if not missing_ranks:
        for pid in (*send, *receive):
            rank = players[pid].player.public_rank
            if rank is None:
                raise DataError(f"public_rank.{pid}: required rank is missing")
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
    return TradeSummary(
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
        acceptance_unavailable=(
            "缺公開排名：" + ", ".join(missing_ranks) + "；接受率與期望值無法計算"
            if missing_ranks
            else None
        ),
        rank_delta=rank_delta.result if rank_delta else None,
        acceptance=accepted.result if accepted else None,
        expected_gain=gain.result if gain else None,
        calibrated=sim.acceptance_fit is not None,
    ), tuple(
        (*traces, *(t for t in (rank_delta, accepted, gain) if t is not None), playoffs, games)
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
    if not effects.details:
        raise DataError("trade.details: complete forecasts are required for a detailed result")
    summary, traces = trade_summary(sim, mine, opponent, send, receive, effects=effects)
    return TradeResult(
        **summary.model_dump(),
        category_changes=category_changes(effects.before, effects.after),
        opponent_category_changes=category_changes(effects.opponent_before, effects.opponent_after),
        before=effects.before,
        after=effects.after,
        opponent_before=effects.opponent_before,
        opponent_after=effects.opponent_after,
        traces=traces,
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
    return evaluate_trade_bundles(
        sim, eligible_trade_bundles(sim, preferences, opponent, size), size, progress
    )


def eligible_trade_bundles(
    sim: Simulation, preferences: InseasonPreferences, opponent: str | None, size: int
) -> tuple[TradeCandidate, ...]:
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
    values = {
        p.player.id: evaluate(
            "rank_value",
            scale=sim.params.rank_scale.value,
            rank=float(p.player.public_rank),
            exponent=sim.params.rank_exponent.value,
        ).result
        for p in sim.projection(sim.as_of.astimezone(sim.zone).date()).players
        if p.player.public_rank is not None
    }
    filtered: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    sim.trade_search_counts = {
        "candidates": len(candidates),
        "value_filtered": 0,
        "unknown_value": 0,
        "full_effects": 0,
        "bounded": 0,
    }
    for candidate in candidates:
        sim.check_limits()
        _, send, receive = candidate
        if any(p not in values for p in (*send, *receive)):
            sim.trade_search_counts["unknown_value"] += 1
        elif (
            evaluate(
                "trade_value_ratio",
                home=fsum(values[p] for p in send),
                away=fsum(values[p] for p in receive),
            ).result
            + sim.params.tolerance.value
            < preferences.trade_value_min_ratio
        ):
            sim.trade_search_counts["value_filtered"] += 1
        else:
            filtered.append(candidate)
    candidates = filtered
    sim.trade_search_counts["eligible"] = len(candidates)
    return tuple(candidates)


@overload
def evaluate_trade_bundles(
    sim: Simulation,
    bundles: tuple[TradeCandidate, ...],
    size: int,
    progress: Callable[[float], None],
    *,
    details: Literal[True] = True,
    on_candidate: Callable[[TradeSummary | None], None] | None = None,
) -> tuple[TradeResult, ...]: ...


@overload
def evaluate_trade_bundles(
    sim: Simulation,
    bundles: tuple[TradeCandidate, ...],
    size: int,
    progress: Callable[[float], None],
    *,
    details: Literal[False],
    on_candidate: Callable[[TradeSummary | None], None] | None = None,
) -> tuple[TradeSummary, ...]: ...


def evaluate_trade_bundles(
    sim: Simulation,
    bundles: tuple[TradeCandidate, ...],
    size: int,
    progress: Callable[[float], None],
    *,
    details: bool = True,
    on_candidate: Callable[[TradeSummary | None], None] | None = None,
) -> tuple[TradeSummary, ...]:
    candidates = list(bundles)
    sim.trade_search_counts.update(eligible=len(candidates), full_effects=0, bounded=0)
    mine = sim.snapshot.mine
    teams = tuple(t for t in sim.snapshot.teams if any(row[0] == t.id for row in candidates))
    if not candidates:
        progress(1.0)
        return ()
    result: list[TradeSummary] = []

    def finished(index: int, trade: TradeSummary | None = None, *, bounded: bool = False) -> None:
        # Publish only resolved candidates, never an interrupted trade's effects.
        sim.trade_search_counts["bounded" if bounded else "full_effects"] += 1
        if on_candidate is not None:
            on_candidate(trade)
        progress((index + 1) / len(candidates))

    before = season_forecasts(sim, mine) if size > 1 else ()
    opponent_before = {t.id: season_forecasts(sim, t.id) for t in teams} if size > 1 else {}
    own_limits = season_limits(sim, before) if size > 1 else None
    other_limits = {t: season_limits(sim, row) for t, row in opponent_before.items()}
    bounds = (
        {
            (team, send, receive): gain_bound(
                sim,
                before,
                opponent_before[team],
                send,
                receive,
                own_limits=own_limits,
                other_limits=other_limits[team],
            )
            for team, send, receive in candidates
        }
        if size > 1
        else {}
    )
    if size > 1:
        candidates.sort(
            key=lambda row: (
                -(value if (value := bounds[row]) is not None else float("inf")),
                row,
            )
        )
    for index, (team, send, receive) in enumerate(candidates):
        sim.check_limits()
        if size > 1:
            upper = bounds[team, send, receive]
            scored = sorted(
                (r.expected_gain for r in result if r.expected_gain is not None), reverse=True
            )
            cutoff = scored[9] if len(scored) >= 10 else None
            if upper is not None and (
                upper <= 0 or (cutoff is not None and upper < cutoff - sim.params.tolerance.value)
            ):
                finished(index, bounded=True)
                continue
        try:
            effects = trade_effects(sim, mine, team, send, receive, details=details)
            # These exact domain constraints can safely prune a completed bundle.
            # A losing smaller bundle cannot prune its supersets: category synergy
            # and unequal-roster replacement make that bound unsound.
            if size > 1 and (effects.own_delta.result <= 0 or effects.other_delta.result < 0):
                finished(index)
                continue
            trade = (
                evaluate_trade(sim, mine, team, send, receive, effects=effects)
                if details
                else trade_summary(sim, mine, team, send, receive, effects=effects)[0]
            )
        except IneligibleTrade:
            finished(index)
            continue
        result.append(trade)
        finished(index, trade)
    return rank_trades(sim, result)


def rank_trades[T: TradeSummary](sim: Simulation, result: Iterable[T]) -> tuple[T, ...]:
    return tuple(
        sorted(
            result,
            key=lambda r: (
                r.expected_gain is None,
                -round(
                    (r.expected_gain if r.expected_gain is not None else r.mine_delta)
                    / sim.params.tolerance.value
                ),
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
