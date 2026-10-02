"""Sound pre-simulation bounds for positive-gain multi-player trade searches."""

from datetime import date, datetime, timedelta
from math import fsum, isfinite
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from fba.contracts.config import Linear
from fba.contracts.inseason import WeekForecast
from fba.formulas.registry import evaluate
from fba.formulas.scalar import upper_total
from fba.formulas.simulation import outward_interval, subset_interval
from fba.inseason.lineup_bounds import grouped_interval, score_ceiling, score_ceilings
from fba.inseason.matchup import Simulation
from fba.inseason.season import remaining_weeks, season_opponent

type Array = NDArray[np.float64]


class WeekDrawBound(NamedTuple):
    lower: Array
    upper: Array
    opponent: Array
    after: datetime
    days: tuple[date, ...]


def completion_contexts(
    sim: Simulation,
    team: str,
    roster: tuple[str, ...],
    changed: dict[str, tuple[str, ...]] | None = None,
) -> tuple[WeekDrawBound, ...]:
    result: list[WeekDrawBound] = []
    today = sim.as_of.astimezone(sim.zone).date()
    for week_id in remaining_weeks(sim):
        sim.check_limits()
        week = next(w for w in sim.league.matchups if w.id == week_id)
        actual, through = sim.actual(team, week_id)
        lower, upper = actual.copy(), actual.copy()
        opponent_id = season_opponent(sim, team, week_id)
        opponent, _ = sim.total(opponent_id, week_id, (changed or {}).get(opponent_id))
        after = max(through, sim.as_of)
        days: list[date] = []
        on = max(week.start, today)
        terms = 0
        while on <= week.end:
            sim.check_limits()
            # Relax slots and eligibility: any subset of these draws is
            # enclosed, including every legal daily/weekly assignment.
            for draw in sim.daily_draws(roster, on, after).values():
                terms += 1
                lower, upper = subset_interval(
                    {"lower": lower, "upper": upper, "draw": draw, "fixed": np.asarray(0.0)}
                )
            lower, upper = outward_interval({"lower": lower, "upper": upper})
            days.append(on)
            on += timedelta(days=1)
        lower, upper = grouped_interval(lower, upper, terms)
        result.append(WeekDrawBound(lower, upper, opponent, after, tuple(days)))
    return tuple(result)


def free_agent_bounds(
    sim: Simulation, team: str, roster: tuple[str, ...], choices: tuple[str, ...]
) -> dict[str, float] | None:
    """Bound every add choice using the same draws, without solving its lineups."""
    sim = sim.season()
    sim.check_limits()
    injured = next(t.injury_players for t in sim.snapshot.teams if t.id == team)
    if (
        len(choices) < 2
        or sim.transitions.get(team)
        or (sim.project_injury_returns and injured)
        or any(
            not isinstance(c.formula, Linear) and c.formula.zero_denominator == "error"
            for c in sim.league.categories
        )
    ):
        # A return/transition can introduce players outside this envelope.
        # Error-producing categories retain the original evaluation order.
        return None
    contexts = completion_contexts(sim, team, roster)
    result: dict[str, float] = {}
    width = sim.params.lineup_batch.value
    for start in range(0, len(choices), width):
        batch = choices[start : start + width]
        values: dict[str, list[float]] = {player: [] for player in batch}
        for context in contexts:
            for player, ceiling in zip(batch, addition_ceilings(sim, context, batch), strict=True):
                values[player].append(ceiling)
        for player, scores in values.items():
            value = upper_total({"values": tuple(scores)})
            if not isfinite(value):
                return None  # No certified bound: retain full comparison, never guess a score.
            result[player] = value
    sim.check_limits()
    return result


def addition_ceilings(
    sim: Simulation, context: WeekDrawBound, choices: tuple[str, ...]
) -> tuple[float, ...]:
    shape = (len(choices), *context.lower.shape)
    lower = np.broadcast_to(context.lower, shape).copy()
    upper = np.broadcast_to(context.upper, shape).copy()
    terms = 0
    for on in context.days:
        sim.check_limits()
        draws = sim.daily_draws(choices, on, context.after)
        indices = [i for i, player in enumerate(choices) if player in draws]
        if indices:
            terms += 1
            daily = np.stack([draws[choices[i]] for i in indices])
            # Each row retains its date/addition order and outward rounding.
            # Only independent candidates share a batch.
            lower[indices], upper[indices] = subset_interval(
                {
                    "lower": lower[indices],
                    "upper": upper[indices],
                    "draw": daily,
                    "fixed": np.asarray(0.0),
                }
            )
    lower, upper = grouped_interval(lower, upper, terms)
    return tuple(float(v) for v in score_ceilings(sim, lower, upper, context.opponent))


def season_limits(sim: Simulation, forecasts: tuple[WeekForecast, ...]) -> tuple[float, float]:
    scale = 1.0 if sim.league.scoring == "h2h_one_win" else float(len(sim.league.categories))
    minimum, maximum = (sim.calibrated_score(raw).result for raw in (0.0, scale))
    dates = {g.tipoff.astimezone(sim.zone).date() for g in sim.games if g.tipoff > sim.as_of}
    bounds: list[tuple[float, float]] = []
    for forecast in forecasts:
        week = next(w for w in sim.league.matchups if w.id == forecast.week_id)
        # With no remaining NBA game anywhere in this week, no traded player
        # or free agent can change its already credited score.
        bounds.append(
            (minimum, maximum)
            if any(week.start <= on <= week.end for on in dates)
            else (forecast.score, forecast.score)
        )
    return fsum(row[0] for row in bounds), fsum(row[1] for row in bounds)


def gain_bound(
    sim: Simulation,
    own: tuple[WeekForecast, ...],
    other: tuple[WeekForecast, ...],
    send: tuple[str, ...],
    receive: tuple[str, ...],
    *,
    own_limits: tuple[float, float] | None = None,
    other_limits: tuple[float, float] | None = None,
) -> float | None:
    _, maximum = own_limits if own_limits is not None else season_limits(sim, own)
    tighter = roster_ceiling(sim, own, other, send, receive)
    if tighter is not None:
        maximum = min(maximum, tighter)
    own_upper = evaluate("difference", before=fsum(w.score for w in own), after=maximum).result
    if own_upper <= 0:
        return 0.0  # A strictly positive-gain bundle is mathematically impossible.
    ranks = {
        p.player.id: p.player.public_rank
        for p in sim.projection(sim.as_of.astimezone(sim.zone).date()).players
    }
    if any(ranks[p] is None for p in (*send, *receive)):
        return None
    values: dict[str, float] = {}
    for pid in (*send, *receive):
        rank = ranks[pid]
        if rank is None:
            return None
        values[pid] = evaluate(
            "rank_value",
            rank=float(rank),
            scale=sim.params.rank_scale.value,
            exponent=sim.params.rank_exponent.value,
        ).result
    rank_delta = evaluate(
        "difference", after=fsum(values[p] for p in send), before=fsum(values[p] for p in receive)
    ).result
    fitted = sim.acceptance_fit or {
        "beta_rank": sim.params.beta_rank.value,
        "beta_need": sim.params.beta_need.value,
        "threshold": sim.params.acceptance_threshold.value,
        "noise": sim.params.acceptance_noise.value,
    }
    minimum, maximum = other_limits if other_limits is not None else season_limits(sim, other)
    need_bound = evaluate(
        "difference",
        before=fsum(w.score for w in other),
        after=maximum if fitted["beta_need"] >= 0 else minimum,
    ).result
    probability = evaluate(
        "acceptance",
        beta_rank=fitted["beta_rank"],
        beta_need=fitted["beta_need"],
        delta_rank=rank_delta,
        delta_need=need_bound,
        threshold=fitted["threshold"],
        noise=fitted["noise"],
    ).result
    return evaluate("product", gain=own_upper, probability=probability).result


def roster_ceiling(
    sim: Simulation,
    own: tuple[WeekForecast, ...],
    other: tuple[WeekForecast, ...],
    send: tuple[str, ...],
    receive: tuple[str, ...],
) -> float | None:
    """Bound every final lineup for balanced exchanges and one required drop."""
    sim = sim.season()
    if not own or not other:
        return None
    team, partner = own[0].home, other[0].home
    roster = tuple(p for p in sim.roster(team) if p not in send) + receive
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    if (
        len(roster) not in (capacity, capacity + 1)
        or sim.transitions.get(team)
        or (
            sim.project_injury_returns
            and next(t.injury_players for t in sim.snapshot.teams if t.id == team)
        )
        or any(
            not isinstance(c.formula, Linear) and c.formula.zero_denominator == "error"
            for c in sim.league.categories
        )
    ):
        return None
    changed = {partner: tuple(p for p in sim.roster(partner) if p not in receive) + send}
    if len(changed[partner]) != capacity and any(
        season_opponent(sim, team, w.week_id) == partner for w in own
    ):
        # Unknown automatic add/drop choices can change our actual opponent.
        return None
    candidates = (
        (roster,)
        if len(roster) == capacity
        else tuple(
            tuple(p for p in roster if p != drop)
            for drop in roster
            if team != sim.snapshot.mine or drop not in sim.untouchable
        )
    )
    upper = 0.0
    for candidate in candidates:
        sim.check_limits()
        contexts = completion_contexts(sim, team, candidate, changed)
        value = upper_total(
            {"values": tuple(score_ceiling(sim, c.lower, c.upper, c.opponent) for c in contexts)}
        )
        if not isfinite(value):
            return None
        upper = max(upper, value)
    return upper
