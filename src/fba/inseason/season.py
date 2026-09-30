from datetime import date
from math import fsum

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.inseason import WeekForecast
from fba.inseason.matchup import Simulation


def remaining_weeks(sim: Simulation, *, after: date | None = None) -> tuple[str, ...]:
    today = after if after is not None else sim.as_of.astimezone(sim.zone).date()
    return tuple(w.id for w in sim.league.matchups if w.phase == "regular" and w.end >= today)


def season_forecasts(
    sim: Simulation,
    team: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
    *,
    after: date | None = None,
) -> tuple[WeekForecast, ...]:
    weeks = remaining_weeks(sim, after=after)
    result: list[WeekForecast] = []
    for week in weeks:
        pair = next(
            (p for p in sim.snapshot.pairings if p.week_id == week and team in (p.home, p.away)),
            None,
        )
        if pair is None:
            raise DataError(f"schedule.{week}.{team}: actual regular-season opponent is missing")
        opponent = pair.away if pair.home == team else pair.home
        result.append(sim.week(team, opponent, week, rosters))
    return tuple(result)


def season_value(
    sim: Simulation,
    team: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
    *,
    after: date | None = None,
) -> float:
    return fsum(w.score for w in season_forecasts(sim, team, rosters, after=after))


def playoff_probability(
    sim: Simulation, rosters: dict[str, tuple[str, ...]] | None = None
) -> dict[str, float]:
    teams = tuple(sorted(sim.snapshot.teams, key=lambda t: t.id))
    scores = {
        t.id: np.full(sim.samples, t.wins + sim.league.week_tie_value * t.ties) for t in teams
    }
    changed = rosters or {}
    weeks = set(remaining_weeks(sim))
    covered: set[tuple[str, str]] = set()
    for pair in sim.snapshot.pairings:
        if pair.week_id not in weeks:
            continue
        a, b, _ = sim.matchup_totals(pair.home, pair.away, pair.week_id, changed)
        scores[pair.home] += sim.score(a, b)[1]
        scores[pair.away] += sim.score(b, a)[1]
        covered.update(((pair.week_id, pair.home), (pair.week_id, pair.away)))
    missing = {(w, t.id) for w in weeks for t in teams} - covered
    if missing:
        raise DataError(f"playoff.schedule: missing matchups {sorted(missing)}")
    # Existing seed is the explicit final tie breaker. No platform-dependent
    # unstable ordering or tiny floating point differences determine a seed.
    values = np.stack([scores[t.id] for t in teams])
    quantized = np.rint(values / sim.params.tolerance.value)
    ranks = np.empty_like(values)
    for column in range(sim.samples):
        order = sorted(
            range(len(teams)), key=lambda i: (-quantized[i, column], teams[i].seed, teams[i].id)
        )
        for rank, index in enumerate(order):
            ranks[index, column] = rank
    return {t.id: float((ranks[i] < sim.league.playoff_teams).mean()) for i, t in enumerate(teams)}
