from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fba.contracts.config import Matchup, StarterSlot
from fba.contracts.inseason import (
    ActualScore,
    AdjustmentLedger,
    BoxScore,
    FantasyTeam,
    FreeAgent,
    FrozenPriors,
    InseasonLeague,
    InseasonParameters,
    LeagueSnapshot,
    PlayerPrior,
    PlayerSnapshot,
    SeasonGame,
    SeasonPairing,
    SeasonPlayer,
)
from fba.contracts.yahoo import YahooCatalog
from fba.inseason.matchup import Simulation

DEFAULTS = Path(__file__).parents[1] / "src/fba/apps/inseason/defaults"


def parameters():
    params = InseasonParameters.model_validate_json((DEFAULTS / "parameters.json").read_bytes())
    return params.model_copy(
        update={
            "simulations": params.simulations.model_copy(update={"value": 100}),
            "season_simulations": params.season_simulations.model_copy(update={"value": 40}),
            "budgets": {k: p.model_copy(update={"value": 30.0}) for k, p in params.budgets.items()},
        }
    )


def fixture(mode="h2h_one_win", year=2026, teams=2):
    now = datetime(year, 10, 13, 8, tzinfo=UTC)
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    start = date(year, 10, 1)
    weeks = tuple(
        Matchup(
            id=str(i + 1),
            start=start + timedelta(days=7 * i),
            end=start + timedelta(days=7 * i + 6),
            phase="regular" if i < 3 else "playoff",
        )
        for i in range(4)
    )
    cats = tuple(
        catalog.category_labels[k]
        for k in ("PTS", "REB", "AST", "STL", "BLK", "3PM", "FG%", "FT%", "TO")
    )
    league = InseasonLeague(
        format_version=1,
        league_id="fixture-league",
        game_key="fixture-game",
        season_id=str(year),
        name="離線測試聯盟",
        teams=teams,
        timezone="America/New_York",
        starts_on=start,
        ends_on=weeks[-1].end,
        positions=("PG", "C"),
        starter_slots=(
            StarterSlot(id="guard", label="Guard", eligible_positions=("PG",)),
            StarterSlot(id="big", label="Big", eligible_positions=("C",)),
        ),
        bench_slots=1,
        injury_slots=(),
        categories=cats,
        base_stats=catalog.base_stats,
        shots=catalog.shots,
        derived=catalog.derived,
        scoring=mode,
        category_ties="use_tie_value",
        week_tie_value=0.5,
        adds_per_week=3,
        effective="same_day",
        cutoff_local_time=datetime.strptime("23:59:59", "%H:%M:%S").time(),
        waiver_days=2,
        lineup_lock="player_game",
        lineup_lock_time=datetime.strptime("00:00", "%H:%M").time(),
        matchups=weeks,
        playoff_teams=1,
        playoff_weeks=("4",),
        trade_deadline=None,
        players_on_court=5,
        regulation_minutes=48,
        yahoo_settings_sha256="a" * 64,
    )
    base = {
        "FGA": 10.0,
        "FGM": 5.0,
        "FTA": 4.0,
        "FTM": 3.0,
        "3PM": 1.0,
        "REB": 6.0,
        "OREB": 2.0,
        "AST": 3.0,
        "TO": 2.0,
        "STL": 1.0,
        "BLK": 1.0,
    }
    players, priors, boxes, games, rosters = [], [], [], [], []
    count = teams * 3 + 3
    for i in range(count):
        pid, team = f"p{i}", f"NBA{i}"
        positions = ("PG",) if i % 3 == 0 else ("C",) if i % 3 == 1 else ("PG", "C")
        players.append(
            SeasonPlayer(
                id=pid,
                name=f"Player {i}",
                team_id=team,
                positions=positions,
                status="healthy",
                known_at=now - timedelta(days=40),
                public_rank=i + 1,
                ownership=0.5,
                ownership_change=0.02,
            )
        )
        factor = 1 + i / 20
        stats = {k: v * factor for k, v in base.items()}
        priors.append(
            PlayerPrior(
                player_id=pid,
                minutes=30.0,
                rates={s: v / 30 for s, v in stats.items()},
                probabilities={"FG%": 0.5, "FT%": 0.75},
            )
        )
        for n in range(10):
            played = now - timedelta(days=10 - n)
            values = {s: v * (0.8 if n % 2 else 1.2) for s, v in stats.items()}
            boxes.append(
                BoxScore(
                    player_id=pid,
                    game_id=f"past:{pid}:{n}",
                    team_id=team,
                    played_at=played,
                    known_at=played + timedelta(hours=3),
                    minutes=30.0,
                    stats=values,
                )
            )
        for n in range(0, 15, 2):
            tipoff = now + timedelta(days=n, hours=12)
            games.append(
                SeasonGame(
                    id=f"future:{pid}:{n}",
                    home=team,
                    away=f"VISIT{i}",
                    tipoff=tipoff,
                    known_at=now - timedelta(days=20),
                    status="scheduled",
                )
            )
    for i in range(teams):
        rosters.append(
            FantasyTeam(
                id=f"team{i}",
                name=f"Team {i}",
                players=tuple(f"p{3 * i + j}" for j in range(3)),
                injury_players={},
                selected_slots={"guard": f"p{3 * i}", "big": f"p{3 * i + 1}"},
                adds_used=0,
                wins=1.0,
                losses=0.0,
                ties=0.0,
                seed=i + 1,
            )
        )
    axes = (*league.base_stats, *(d.id for d in league.derived))
    actual = tuple(
        ActualScore(
            week_id=w.id,
            team_id=t.id,
            through=now,
            totals=dict.fromkeys(axes, 0.0),
            final=w.end < now.date(),
        )
        for w in weeks
        if w.start <= now.date()
        for t in rosters
    )
    pairings = tuple(
        SeasonPairing(week_id=w.id, home=f"team{i}", away=f"team{i + 1}")
        for w in weeks
        for i in range(0, teams, 2)
    )
    snapshot = LeagueSnapshot(
        format_version=1,
        league_id=league.league_id,
        as_of=now,
        settings_sha256=league.yahoo_settings_sha256,
        mine="team0",
        teams=tuple(rosters),
        pairings=pairings,
        actual=actual,
        free_agents=tuple(
            FreeAgent(player_id=f"p{i}", status="free", clears_at=None)
            for i in range(teams * 3, count)
        ),
        unresolved_rostered=(),
        unresolved_free=(),
    )
    player_snapshot = PlayerSnapshot(
        format_version=1,
        season_id=str(year),
        source="synthetic-fixture",
        as_of=now,
        players=tuple(players),
        games=tuple(games),
        boxes=tuple(boxes),
    )
    frozen = FrozenPriors(
        format_version=1,
        season_id=str(year),
        version="fixture-v1",
        source_sha256="b" * 64,
        known_at=now - timedelta(days=40),
        players=tuple(priors),
    )
    return (
        league,
        parameters(),
        player_snapshot,
        frozen,
        AdjustmentLedger(format_version=1, entries=()),
        snapshot,
        now,
    )


def simulation(**kwargs):
    return Simulation(*fixture(**kwargs))
