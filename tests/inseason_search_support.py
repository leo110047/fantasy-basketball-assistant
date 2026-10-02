"""Fixed F3 counterexamples with production samples and search parameters.

Two five-player rosters, five free agents, and three three-day matchups.
The first six seeds were diagnosed before changing the search policy.
"""

from datetime import timedelta

import numpy as np
from inseason_support import DEFAULTS, fixture

from fba.contracts.config import Matchup
from fba.contracts.inseason import FreeAgent, InseasonParameters, InseasonPreferences
from fba.inseason.matchup import Simulation


def search_scenario(seed, mode, adds):
    league, _, players, priors, ledger, snapshot, now = fixture(mode=mode, teams=4)
    params = InseasonParameters.model_validate_json((DEFAULTS / "parameters.json").read_bytes())
    params = params.model_copy(update={"seed": params.seed.model_copy(update={"value": seed})})
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(
        update={"reserve_adds": 0, "untouchable": ("p0",) if seed % 4 == 3 else ()}
    )
    weeks = tuple(
        Matchup(
            id=str(n + 2),
            start=now.date() + timedelta(days=3 * n),
            end=now.date() + timedelta(days=3 * n + 2),
            phase="regular" if n < 2 else "playoff",
        )
        for n in range(3)
    )
    league = league.model_copy(
        update={
            "teams": 2,
            "bench_slots": 3,
            "adds_per_week": adds,
            "matchups": weeks,
            "ends_on": weeks[-1].end,
            "playoff_weeks": ("4",),
            "effective": "next_day" if seed % 4 == 1 else "same_day",
        }
    )
    rng = np.random.default_rng(20261002 + seed)
    factors = {
        p.id: {
            s: float(rng.uniform(0.35, 2.4))
            for s in ("FGA", "FTA", "REB", "OREB", "AST", "TO", "STL", "BLK")
        }
        for p in players.players
    }

    def stats(pid, original):
        result = {s: float(v) * factors[pid].get(s, 1) for s, v in original.items()}
        result["FGM"] = result["FGA"] * (0.35 + 0.25 * ((int(pid[1:]) + seed) % 5) / 4)
        result["FTM"] = result["FTA"] * (0.55 + 0.35 * ((int(pid[1:]) + seed) % 7) / 6)
        result["3PM"] = min(result["FGM"], original.get("3PM", 1) * factors[pid]["FGA"])
        return result

    updated_priors = []
    for prior in priors.players:
        rates = stats(prior.player_id, prior.rates)
        updated_priors.append(
            prior.model_copy(
                update={
                    "rates": rates,
                    "probabilities": {
                        "FG%": rates["FGM"] / rates["FGA"],
                        "FT%": rates["FTM"] / rates["FTA"],
                    },
                }
            )
        )
    priors = priors.model_copy(update={"players": tuple(updated_priors)})
    first_tipoff = min(g.tipoff for g in players.games)
    games = tuple(
        g.model_copy(
            update={"id": f"{g.id}:s{seed}:d{day}", "tipoff": now + timedelta(days=day, hours=12)}
        )
        for g in players.games
        if g.tipoff == first_tipoff
        for day in range(9)
        if (day + int(g.home[3:]) + seed) % 3 != 0
    )
    players = players.model_copy(
        update={
            "games": games,
            "boxes": tuple(
                b.model_copy(update={"stats": stats(b.player_id, b.stats)}) for b in players.boxes
            ),
        }
    )
    teams = (
        snapshot.teams[0].model_copy(update={"players": tuple(f"p{i}" for i in range(5))}),
        snapshot.teams[1].model_copy(
            update={
                "players": tuple(f"p{i}" for i in range(5, 10)),
                "selected_slots": {"guard": "p6", "big": "p7"},
            }
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "teams": teams,
            "pairings": tuple(
                p.model_copy(update={"week_id": w.id})
                for w in weeks
                for p in snapshot.pairings
                if p.week_id == "2" and p.home == "team0"
            ),
            "actual": tuple(
                s for s in snapshot.actual if s.week_id == "2" and s.team_id in ("team0", "team1")
            ),
            "free_agents": tuple(
                FreeAgent(
                    player_id=f"p{i}",
                    status="waiver" if i == 10 and seed % 4 == 2 else "free",
                    clears_at=now + timedelta(days=1) if i == 10 and seed % 4 == 2 else None,
                )
                for i in range(10, 15)
            ),
        }
    )
    return Simulation(
        league, params, players, priors, ledger, snapshot, now, untouchable=prefs.untouchable
    ), prefs
