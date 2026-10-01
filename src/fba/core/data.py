from collections import Counter

from fba.contracts.base import DataError, IdentityError
from fba.contracts.data import (
    Game,
    IdentityMap,
    Player,
    ProviderPlayer,
    RosterRow,
    ScheduleCount,
)


def resolve_players(
    rows: tuple[RosterRow, ...],
    identities: IdentityMap,
    provider_players: tuple[ProviderPlayer, ...],
    history_ids: frozenset[str],
    incomplete_ids: frozenset[str],
) -> tuple[Player, ...]:
    roster_ids = {r.id for r in rows}
    if len(roster_ids) != len(rows):
        raise DataError("roster: duplicate player IDs")
    available = {(p.provider, p.id): p for p in provider_players}
    if len(available) != len(provider_players):
        raise DataError("provider roster: duplicate player IDs")
    mappings = {(i.player_id, i.provider): i for i in identities.entries}
    targets = {(i.provider, i.provider_player_id) for i in identities.entries}
    if len(mappings) != len(identities.entries) or len(targets) != len(identities.entries):
        raise DataError("identity_map: duplicate or conflicting identities")
    players: list[Player] = []
    unresolved: list[str] = []
    for row in sorted(rows, key=lambda r: r.id):
        mapped = tuple(i for i in identities.entries if i.player_id == row.id)
        matches = [available.get((i.provider, i.provider_player_id)) for i in mapped]
        if not matches or any(p is None for p in matches):
            unresolved.append(row.id)
            continue
        teams = {p.team_id for p in matches if p is not None}
        if len(teams) != 1:
            raise DataError(f"identity_map.{row.id}: providers disagree about team")
        players.append(
            Player(
                roster=row,
                identities=tuple(sorted(mapped, key=lambda i: i.provider)),
                team_id=next(iter(teams)),
                history_status="incomplete"
                if row.id in incomplete_ids
                else "available"
                if row.id in history_ids
                else "no_previous_season_history",
            )
        )
    if unresolved:
        raise IdentityError(tuple(unresolved))
    return tuple(players)


def validate_schedule(games: tuple[Game, ...], counts: tuple[ScheduleCount, ...]) -> None:
    if not games or not counts:
        raise DataError("schedule: games and official counts are required")
    if len({g.id for g in games}) != len(games):
        raise DataError("schedule: duplicate game IDs")
    if len({c.team_id for c in counts}) != len(counts):
        raise DataError("official_schedule_counts: duplicate team IDs")
    by_team: Counter[str] = Counter()
    team_dates: set[tuple[str, str]] = set()
    for game in games:
        if game.home_team_id == game.away_team_id:
            raise DataError(f"schedule.{game.id}: home and away must differ")
        if game.status in ("postponed", "cancelled"):
            continue
        for team in (game.home_team_id, game.away_team_id):
            day = (team, game.local_date.isoformat())
            if day in team_dates:
                raise DataError(f"schedule.{game.id}: duplicate team date {day}")
            team_dates.add(day)
            by_team[team] += 1
    if set(by_team) != {c.team_id for c in counts}:
        raise DataError("schedule: team IDs disagree with official counts")
    for count in counts:
        if by_team[count.team_id] != count.announced:
            raise DataError(f"schedule.{count.team_id}: announced game count mismatch")
        if (count.pending > 0) != (count.pending_reason is not None):
            raise DataError(f"schedule.{count.team_id}: pending games require a reason")
