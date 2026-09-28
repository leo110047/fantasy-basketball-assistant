import re
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from fba.adapters.codec import checked_json
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import Source
from fba.contracts.data import (
    ActualGames,
    ActualSeason,
    Forecast,
    Game,
    PlayerGame,
    ProviderPlayer,
    StatValue,
)


class Wire(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", allow_inf_nan=False)


class EspnStats(Wire):
    seasonId: int
    statSourceId: int
    statSplitTypeId: int
    stats: dict[str, float | Literal["Infinity"]]
    externalId: str
    proTeamId: int

    @field_validator("stats")
    @classmethod
    def validate_ratios(
        _cls, value: dict[str, float | Literal["Infinity"]]
    ) -> dict[str, float | Literal["Infinity"]]:
        if any(isinstance(v, str) and k not in ("35", "36") for k, v in value.items()):
            raise ValueError("Infinity is supported only in ESPN ratio fields 35 and 36")
        return value


class EspnPlayer(Wire):
    id: int
    fullName: str
    proTeamId: int
    stats: tuple[EspnStats, ...] = ()


class LeaguePlayer(Wire):
    player: EspnPlayer


class LeaguePlayers(Wire):
    players: tuple[LeaguePlayer, ...]


class EspnGame(Wire):
    id: int
    date: int
    homeProTeamId: int
    awayProTeamId: int
    postponed: bool
    detail: str


class EspnTeam(Wire):
    id: int
    abbrev: str
    proGamesByScoringPeriod: dict[str, tuple[EspnGame, ...]] = {}


class EspnSettings(Wire):
    proTeams: tuple[EspnTeam, ...]


class EspnSchedule(Wire):
    settings: EspnSettings


def validate_source_season(source: Source) -> None:
    match = re.fullmatch(r"(\d{4})-(\d{2})", source.season_id)
    if match is None or int(match[2]) != (int(match[1]) + 1) % 100:
        raise ConfigError(f"season.sources.{source.id}.season_id: expected YYYY-YY NBA season")
    expected = str(int(match[1]) + 1)
    if source.season_code != expected:
        raise ConfigError(f"season.sources.{source.id}.season_code: expected {expected}")


def decode_players(data: bytes, label: str) -> tuple[EspnPlayer, ...]:
    try:
        wire = TypeAdapter[tuple[EspnPlayer, ...] | LeaguePlayers](
            tuple[EspnPlayer, ...] | LeaguePlayers
        ).validate_json(checked_json(data, label))
    except ValidationError as exc:
        raise DataError(f"{label}: ESPN player format changed: {exc}") from exc
    players = tuple(row.player for row in wire.players) if isinstance(wire, LeaguePlayers) else wire
    if not players or len({p.id for p in players}) != len(players):
        raise DataError(f"{label}: empty or duplicate ESPN player IDs")
    return players


def stat_values(
    stats: dict[str, float | Literal["Infinity"]], label: str, *, partial: bool
) -> tuple[StatValue, ...]:
    # ESPN wire IDs are adapter-owned basketball statistics, not league categories.
    fields = (
        ("FGM", "13"),
        ("FGA", "14"),
        ("FTM", "15"),
        ("FTA", "16"),
        ("PTS", "0"),
        ("3PM", "17"),
        ("REB", "6"),
        ("OREB", "4"),
        ("AST", "3"),
        ("TO", "11"),
        ("STL", "2"),
        ("BLK", "1"),
        ("MIN", "40"),
    )
    numeric = {k: v for k, v in stats.items() if not isinstance(v, str)}
    if any(numeric[key] < 0 for _, key in fields if key in numeric):
        raise DataError(f"{label}: negative basketball statistic")
    for made, attempted in (("13", "14"), ("15", "16"), ("17", "13"), ("4", "6")):
        if made in numeric and attempted in numeric and numeric[made] > numeric[attempted]:
            raise DataError(f"{label}: ESPN stat {made} exceeds {attempted}")
    if all(k in numeric for k in ("0", "13", "17", "15")):
        if abs(numeric["0"] - (2 * numeric["13"] + numeric["17"] + numeric["15"])) > 1e-8:
            raise DataError(f"{label}: PTS != 2*FGM + 3PM + FTM")
    missing = [name for name, key in fields if key not in numeric]
    if missing and not partial:
        raise DataError(f"{label}: missing played-game statistics {missing}")
    return tuple(
        sorted(
            (
                StatValue(
                    id=name,
                    value=numeric.get(key),
                    missing_reason="not supplied by ESPN" if key not in numeric else None,
                )
                for name, key in fields
            ),
            key=lambda s: s.id,
        )
    )


def forecasts(players: tuple[EspnPlayer, ...], season: int, source: str) -> tuple[Forecast, ...]:
    result: list[Forecast] = []
    for player in players:
        rows = [
            s
            for s in player.stats
            if (s.seasonId == season and s.statSourceId == 1 and s.statSplitTypeId == 0 and s.stats)
        ]
        if len(rows) > 1:
            raise DataError(f"{source}.{player.id}: ambiguous season projection")
        if rows:
            stats = rows[0].stats
            gp = stats.get("42")
            if gp is None or isinstance(gp, str) or gp < 0:
                raise DataError(f"{source}.{player.id}: missing projected games")
            result.append(
                Forecast(
                    player_id=str(player.id),
                    expected_games=gp,
                    totals=stat_values(stats, f"{source}.{player.id}", partial=True),
                    source_id=source,
                )
            )
    if not result:
        raise DataError(f"{source}: no projections for season {season}")
    return tuple(sorted(result, key=lambda p: p.player_id))


def game_logs(players: tuple[EspnPlayer, ...], season: int, source: str) -> tuple[PlayerGame, ...]:
    result: dict[tuple[str, str], PlayerGame] = {}
    for player in players:
        for row in player.stats:
            if row.seasonId != season or row.statSourceId != 0 or row.statSplitTypeId != 5:
                continue
            if not row.stats:
                continue  # ESPN represents scheduled/DNP rows with an empty stat object.
            game = PlayerGame(
                player_id=str(player.id),
                game_id=row.externalId,
                team_id=str(row.proTeamId),
                stats=stat_values(
                    row.stats, f"{source}.{player.id}.{row.externalId}", partial=False
                ),
                source_id=source,
            )
            key = (game.player_id, game.game_id)
            if key in result and result[key] != game:
                raise DataError(f"{source}: conflicting duplicate game {key}")
            result[key] = game
    if not result:
        raise DataError(f"{source}: no game logs for season {season}")
    return tuple(result[k] for k in sorted(result))


def actual_games(
    players: tuple[EspnPlayer, ...], season: int, source: str
) -> tuple[ActualGames, ...]:
    result: list[ActualGames] = []
    for player in players:
        totals = [
            s
            for s in player.stats
            if (s.seasonId == season and s.statSourceId == 0 and s.statSplitTypeId == 0 and s.stats)
        ]
        if len(totals) > 1:
            raise DataError(f"{source}.{player.id}: ambiguous actual season totals")
        if totals:
            games = totals[0].stats.get("42")
            if games is None or isinstance(games, str) or not games.is_integer() or games < 0:
                raise DataError(f"{source}.{player.id}: invalid actual season games")
            result.append(ActualGames(player_id=str(player.id), games=int(games)))
    if not result:
        raise DataError(f"{source}: missing actual season totals for calibration")
    return tuple(sorted(result, key=lambda p: p.player_id))


def season_totals(
    players: tuple[EspnPlayer, ...], season: int, source: str
) -> tuple[ActualSeason, ...]:
    counts = actual_games(players, season, source)
    by_id = {str(p.id): p for p in players}
    return tuple(
        ActualSeason(
            player_id=p.player_id,
            games=p.games,
            totals=stat_values(
                next(
                    s.stats
                    for s in by_id[p.player_id].stats
                    if s.seasonId == season
                    and s.statSourceId == 0
                    and s.statSplitTypeId == 0
                    and s.stats
                ),
                f"{source}.{p.player_id}.season",
                partial=False,
            ),
        )
        for p in counts
    )


def schedule(data: bytes, season: int, source: str, timezone: str) -> tuple[Game, ...]:
    try:
        wire = EspnSchedule.model_validate_json(checked_json(data, source))
    except ValidationError as exc:
        raise DataError(f"{source}: ESPN schedule format changed: {exc}") from exc
    result: dict[str, Game] = {}
    for team in wire.settings.proTeams:
        for group in team.proGamesByScoringPeriod.values():
            for row in group:
                tipoff = datetime.fromtimestamp(row.date / 1000, tz=UTC)
                game = Game(
                    id=str(row.id),
                    home_team_id=str(row.homeProTeamId),
                    away_team_id=str(row.awayProTeamId),
                    tipoff=tipoff,
                    local_date=tipoff.astimezone(ZoneInfo(timezone)).date(),
                    source_id=source,
                    status="postponed"
                    if row.postponed
                    else "completed"
                    if row.detail.startswith("Final")
                    else "cancelled"
                    if row.detail == "Canceled"
                    else "scheduled",
                )
                if game.id in result and result[game.id] != game:
                    raise DataError(f"{source}: conflicting duplicate game {game.id}")
                result[game.id] = game
    if not result or any(g.local_date.year not in (season - 1, season) for g in result.values()):
        raise DataError(f"{source}: empty schedule or dates outside requested season {season}")
    return tuple(sorted(result.values(), key=lambda g: (g.tipoff, g.id)))


class RosterAthlete(Wire):
    id: str
    fullName: str = Field(validation_alias=AliasChoices("fullName", "name"))


class RosterTeam(Wire):
    id: str


class RosterSeason(Wire):
    year: int


class TeamRoster(Wire):
    team: RosterTeam
    season: RosterSeason = Field(validation_alias=AliasChoices("season", "metadata"))
    athletes: tuple[RosterAthlete, ...]


class RosterPage(Wire):
    roster: TeamRoster = Field(validation_alias=AliasPath("page", "content", "roster"))


def roster_page(data: bytes, season: int, source: str) -> tuple[ProviderPlayer, ...]:
    # Decode the page's provider-owned JSON; never execute embedded JavaScript.
    payloads = re.findall(rb"window\['__espnfitt__'\]=(\{.*?\});</script>", data, re.DOTALL)
    if len(payloads) != 1:
        raise DataError(f"{source}: ESPN roster page payload missing or ambiguous")
    try:
        wire = RosterPage.model_validate_json(checked_json(payloads[0], source))
    except ValidationError as exc:
        raise DataError(f"{source}: ESPN roster page format changed: {exc}") from exc
    return roster_players(wire.roster, season, source)


def roster(data: bytes, season: int, source: str) -> tuple[ProviderPlayer, ...]:
    try:
        wire = TeamRoster.model_validate_json(checked_json(data, source))
    except ValidationError as exc:
        raise DataError(f"{source}: ESPN roster format changed: {exc}") from exc
    return roster_players(wire, season, source)


def roster_players(wire: TeamRoster, season: int, source: str) -> tuple[ProviderPlayer, ...]:
    if wire.season.year != season:
        raise DataError(f"{source}: roster season mismatch")
    if not wire.athletes or len({p.id for p in wire.athletes}) != len(wire.athletes):
        raise DataError(f"{source}: empty or duplicate roster player IDs")
    return tuple(
        ProviderPlayer(
            provider="espn",
            id=p.id,
            name=p.fullName,
            team_id=wire.team.id,
        )
        for p in sorted(wire.athletes, key=lambda p: p.id)
    )
