from datetime import date
from math import fsum

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.inseason import WeekForecast
from fba.inseason.forecast import cached_matchup_points, forecast_score
from fba.inseason.matchup import Simulation


class MissingSeasonOpponent(DataError):
    """A configured remaining week has no known opponent for this team."""


def remaining_weeks(
    sim: Simulation, *, after: date | None = None, include_playoffs: bool = False
) -> tuple[str, ...]:
    today = after if after is not None else sim.as_of.astimezone(sim.zone).date()
    return tuple(
        w.id
        for w in sim.league.matchups
        if (include_playoffs or w.phase == "regular") and w.end >= today
    )


def season_forecasts(
    sim: Simulation,
    team: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
    *,
    after: date | None = None,
    include_playoffs: bool = False,
) -> tuple[WeekForecast, ...]:
    sim = sim.season()
    weeks = remaining_weeks(sim, after=after, include_playoffs=include_playoffs)
    result: list[WeekForecast] = []
    for week in weeks:
        result.append(sim.week(team, season_opponent(sim, team, week), week, rosters))
    return tuple(result)


def season_opponent(sim: Simulation, team: str, week: str) -> str:
    pair = next(
        (p for p in sim.snapshot.pairings if p.week_id == week and team in (p.home, p.away)), None
    )
    if pair is None:
        raise MissingSeasonOpponent(f"schedule.{week}.{team}: actual remaining opponent is missing")
    return pair.away if pair.home == team else pair.home


def season_value(
    sim: Simulation,
    team: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
    *,
    after: date | None = None,
    include_playoffs: bool = False,
) -> float:
    sim = sim.season()
    sim.check_limits()
    weeks = remaining_weeks(sim, after=after, include_playoffs=include_playoffs)
    opponents = tuple((week, season_opponent(sim, team, week)) for week in weeks)
    involved = {team, *(opponent for _, opponent in opponents)}
    changed = rosters or {}
    key = (
        team,
        opponents,
        tuple(
            (t.id, tuple(sorted(changed.get(t.id, t.players))))
            for t in sim.snapshot.teams
            if t.id in involved
        ),
    )
    if key in sim.season_score_cache:
        sim.season_score_cache.move_to_end(key)
        return sim.season_score_cache[key]
    value = fsum(forecast_score(sim, team, opponent, week, rosters) for week, opponent in opponents)
    sim.season_score_cache[key] = value
    if len(sim.season_score_cache) > sim.params.scenario_cache_entries.value:
        sim.season_score_cache.popitem(last=False)
    return value


def playoff_probability(
    sim: Simulation, rosters: dict[str, tuple[str, ...]] | None = None
) -> dict[str, float]:
    sim = sim.season()
    changed = rosters or {}
    if not changed and sim.playoff_probabilities is not None:
        return sim.playoff_probabilities.copy()
    teams = tuple(sorted(sim.snapshot.teams, key=lambda t: t.id))
    scores = {
        t.id: np.full(sim.samples, t.wins + sim.league.week_tie_value * t.ties) for t in teams
    }
    weeks = set(remaining_weeks(sim))
    covered: set[tuple[str, str]] = set()
    for pair in sim.snapshot.pairings:
        if pair.week_id not in weeks:
            continue
        sim.check_limits()
        points = cached_matchup_points(sim, pair.home, pair.away, pair.week_id, changed)
        scores[pair.home] += points[0]
        scores[pair.away] += points[1]
        covered.update(((pair.week_id, pair.home), (pair.week_id, pair.away)))
    missing = {(w, t.id) for w in weeks for t in teams} - covered
    if missing:
        raise DataError(f"playoff.schedule: missing matchups {sorted(missing)}")
    # Existing seed is the explicit final tie breaker. No platform-dependent
    # unstable ordering or tiny floating point differences determine a seed.
    values = np.stack([scores[t.id] for t in teams])
    quantized = np.rint(values / sim.params.tolerance.value)
    indices = np.broadcast_to(np.arange(len(teams))[:, None], values.shape)
    seeds = np.broadcast_to(np.array([t.seed for t in teams])[:, None], values.shape)
    order = np.lexsort((indices, seeds, -quantized), axis=0)
    ranks = np.empty_like(order)
    np.put_along_axis(ranks, order, indices, axis=0)
    result = {
        t.id: float((ranks[i] < sim.league.playoff_teams).mean()) for i, t in enumerate(teams)
    }
    if not changed:
        sim.playoff_probabilities = result.copy()
    return result
