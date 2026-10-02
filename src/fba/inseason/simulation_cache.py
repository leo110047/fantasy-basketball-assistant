"""Bounded in-session reuse across unchanged evidence and lineup-lock intervals."""

import json
from datetime import datetime

from fba.contracts.inseason_app import LeagueState
from fba.data.codec import canonical, digest
from fba.inseason.matchup import Simulation


def reuse_simulation(
    cache: dict[bool, tuple[str, Simulation]],
    season: bool,
    simulation: Simulation,
    state: LeagueState,
) -> Simulation:
    players, league, now = simulation.players, simulation.snapshot, simulation.as_of
    # One bounded cache per sample contract. Reuse only within an unchanged
    # evidence/lock interval; crossing midnight or a known-at/tipoff boundary
    # invalidates forecasts even before the next scheduled sync.
    clocks = sorted(
        {
            *[p.known_at for p in players.players],
            *[b.known_at for b in players.boxes],
            *[g.known_at for g in players.games],
            *[g.tipoff for g in players.games],
            *[e.created_at for e in simulation.ledger.entries],
            *[s.through for s in league.actual],
            datetime.combine(
                now.astimezone(simulation.zone).date(),
                simulation.league.lineup_lock_time,
                simulation.zone,
            ),
        }
    )
    identity = {
        "league": digest(canonical(simulation.league)),
        "parameters": digest(canonical(simulation.params)),
        "ledger": digest(canonical(simulation.ledger)),
        "inputs": [state.players_sha256, state.priors_sha256, state.normalized_sha256],
        "day": now.astimezone(simulation.zone).date().isoformat(),
        "interval": sum(at <= now for at in clocks),
        # Observations use a strict played_at boundary; known_at and locks use <=.
        "played_interval": sum(b.played_at < now for b in players.boxes),
        "untouchable": sorted(simulation.untouchable),
    }
    key = digest(json.dumps(identity, sort_keys=True).encode())
    cached = cache.get(season)
    if cached is not None and cached[0] == key:
        previous = cached[1]
        simulation.draws = previous.draws
        simulation.draw_profiles = previous.draw_profiles
        simulation.projections = {
            on: projection.model_copy(update={"as_of": now})
            for on, projection in previous.projections.items()
        }
        simulation.injury_plan_cache = previous.injury_plan_cache.copy()
        simulation.projection_profiles = {
            key: projection.model_copy(update={"as_of": now})
            for key, projection in previous.projection_profiles.items()
        }
        simulation.joint_weeks = previous.joint_weeks.copy()
        simulation.count_weeks = previous.count_weeks.copy()
        simulation.team_cache = previous.team_cache.copy()
        simulation.expected_cache = previous.expected_cache.copy()
        simulation.matchup_cache = previous.matchup_cache.copy()
        simulation.forecast_cache = previous.forecast_cache.copy()
        simulation.season_score_cache = previous.season_score_cache.copy()
        simulation.standings_point_cache = previous.standings_point_cache.copy()
        simulation.playoff_probabilities = (
            previous.playoff_probabilities.copy()
            if previous.playoff_probabilities is not None
            else None
        )
        simulation.season_engine = previous.season_engine
        if simulation.season_engine is not None:
            child = simulation.season_engine
            # The cached child is private to this request after a shallow
            # copy of its cache maps, keeping changed-roster branches local.
            simulation.season_engine = Simulation(
                child.league,
                child.params,
                child.players,
                child.priors,
                child.ledger,
                child.snapshot,
                now,
                child.samples,
                untouchable=tuple(simulation.untouchable),
            )
            for name in (
                "draws",
                "draw_profiles",
                "projections",
                "projection_profiles",
                "team_cache",
                "expected_cache",
                "matchup_cache",
                "forecast_cache",
                "joint_weeks",
                "count_weeks",
                "injury_plan_cache",
                "standings_point_cache",
                "season_score_cache",
            ):
                setattr(simulation.season_engine, name, getattr(child, name).copy())
            simulation.season_engine.projections = {
                on: p.model_copy(update={"as_of": now}) for on, p in child.projections.items()
            }
            simulation.season_engine.projection_profiles = {
                key: p.model_copy(update={"as_of": now})
                for key, p in child.projection_profiles.items()
            }
    cache[season] = key, simulation
    return simulation
