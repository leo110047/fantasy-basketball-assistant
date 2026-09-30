import json
from datetime import UTC, date, datetime
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from pydantic import JsonValue, TypeAdapter

from fba.contracts.base import DataError
from fba.contracts.inseason import (
    ActualScore,
    FantasyTeam,
    FreeAgent,
    InseasonLeague,
    LeagueSnapshot,
    SeasonPairing,
)
from fba.contracts.yahoo import (
    DiscoveredLeague,
    IdentityMappings,
    LeagueDraft,
    SettingsDifference,
    YahooCatalog,
)
from fba.data.yahoo import SyncBundle, settings_digest, text_at, xml, xml_value


def settings_draft(
    bundle: SyncBundle, selected: DiscoveredLeague, catalog: YahooCatalog
) -> LeagueDraft:
    root = xml(bundle.documents["settings"].encode())
    settings = root.find(".//settings")
    if settings is None:
        raise DataError("Yahoo.settings: missing")
    modes = {"head": "h2h_each_category", "headone": "h2h_one_win"}
    scoring = text_at(settings, "scoring_type")
    if scoring not in modes:
        raise DataError(
            f"Yahoo.scoring_type: unsupported {scoring}; "
            "only H2H One Win and Each Category are supported"
        )
    weeks = xml(bundle.documents["weeks"].encode()).findall(".//game_week")
    playoff_start = int(text_at(settings, "playoff_start_week"))
    matchups: list[JsonValue] = [
        {
            "id": text_at(w, "week"),
            "start": text_at(w, "start"),
            "end": text_at(w, "end"),
            "phase": "regular" if int(text_at(w, "week")) < playoff_start else "playoff",
        }
        for w in weeks
    ]
    if not weeks:
        raise DataError("Yahoo.game_weeks: empty season calendar")
    categories: list[JsonValue] = []
    labels = {
        text_at(stat, "stat_id"): text_at(stat, "display_name")
        for stat in xml(bundle.documents["stat-catalog"].encode()).findall(".//stat")
    }
    for stat in settings.findall("./stat_categories/stats/stat"):
        identifier = text_at(stat, "stat_id")
        label = stat.findtext("display_name") or labels.get(identifier)
        if label is None:
            raise DataError(f"Yahoo.stat_categories.{identifier}: missing game stat metadata")
        if label not in catalog.category_labels:
            raise DataError(
                f"Yahoo.stat_categories.{label}: "
                "add an explicit category definition to provider catalog"
            )
        categories.append(catalog.category_labels[label].model_dump(mode="json"))
    starters: list[JsonValue] = []
    injuries: list[JsonValue] = []
    bench = 0
    for position in settings.findall("./roster_positions/roster_position"):
        label, count = text_at(position, "position"), int(text_at(position, "count"))
        if label in catalog.bench_labels:
            bench += count
        elif label in catalog.injury_labels:
            injuries.append(
                {
                    "id": label,
                    "label": label,
                    "count": count,
                    "eligible_statuses": list(catalog.injury_labels[label]),
                }
            )
        elif label in catalog.positions:
            starters.extend(
                {
                    "id": f"{label}:{i}",
                    "label": label,
                    "eligible_positions": list(catalog.positions[label]),
                }
                for i in range(count)
            )
        else:
            raise DataError(f"Yahoo.roster_positions.{label}: missing position definition")
    document: dict[str, JsonValue] = dict(catalog.confirmation_defaults)
    document.update(
        TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                "format_version": 1,
                "league_id": selected.key,
                "game_key": selected.game_key,
                "season_id": selected.season,
                "name": selected.name,
                "teams": int(text_at(root, ".//num_teams")),
                "starts_on": text_at(weeks[0], "start"),
                "ends_on": text_at(weeks[-1], "end"),
                "positions": sorted(
                    {p for positions in catalog.positions.values() for p in positions}
                ),
                "starter_slots": starters,
                "bench_slots": bench,
                "injury_slots": injuries,
                "categories": categories,
                "base_stats": list(catalog.base_stats),
                "shots": [s.model_dump(mode="json") for s in catalog.shots],
                "derived": [d.model_dump(mode="json") for d in catalog.derived],
                "scoring": modes[scoring],
                "adds_per_week": int(text_at(settings, "max_weekly_adds")),
                "waiver_days": int(text_at(settings, "waiver_time")),
                "matchups": matchups,
                "playoff_teams": int(text_at(settings, "num_playoff_teams")),
                "playoff_weeks": [
                    text_at(w, "week") for w in weeks if int(text_at(w, "week")) >= playoff_start
                ],
                "yahoo_settings_sha256": settings_digest(root),
            }
        )
    )
    return LeagueDraft(
        document=document,
        yahoo_settings=xml_value(settings),
        confirm_fields=tuple(
            sorted((*catalog.confirmation_defaults, "injury_slots", "categories", "playoff_weeks"))
        ),
    )


def differences(
    local: JsonValue, remote: JsonValue, prefix: str = "settings"
) -> tuple[SettingsDifference, ...]:
    if isinstance(local, dict) and isinstance(remote, dict):
        return tuple(
            d
            for key in sorted(local.keys() | remote.keys())
            for d in differences(local.get(key), remote.get(key), prefix + "." + key)
        )
    return () if local == remote else (SettingsDifference(key=prefix, local=local, yahoo=remote),)


def stat_mapping(bundle: SyncBundle, catalog: YahooCatalog) -> dict[str, tuple[str, ...]]:
    root = xml(bundle.documents["stat-catalog"].encode())
    result: dict[str, tuple[str, ...]] = {}
    for stat in root.findall(".//stat"):
        label = stat.findtext("display_name")
        if label in catalog.stat_labels:
            result[text_at(stat, "stat_id")] = catalog.stat_labels[label]
    return result


def totals(team: ET.Element, mapping: dict[str, tuple[str, ...]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for stat in team.findall("./team_stats/stats/stat"):
        key = text_at(stat, "stat_id")
        if key not in mapping:
            continue
        fields = mapping[key]
        raw = text_at(stat, "value").split("/")
        if len(fields) != len(raw):
            raise DataError(f"Yahoo.team_stats.{key}: component count changed")
        try:
            result.update((name, float(value)) for name, value in zip(fields, raw, strict=True))
        except ValueError:
            raise DataError(f"Yahoo.team_stats.{key}: missing or nonnumeric total") from None
    return result


def transaction_adds(
    bundle: SyncBundle, team_id: str, since: date, until: date, zone_name: str
) -> int:
    count = 0
    seen: set[str] = set()
    for key, raw in bundle.documents.items():
        if not key.startswith("transactions:"):
            continue
        for transaction in xml(raw.encode()).findall(".//transaction"):
            identifier = text_at(transaction, "transaction_key")
            if identifier in seen or transaction.findtext("status") != "successful":
                continue
            seen.add(identifier)
            at = (
                datetime.fromtimestamp(int(text_at(transaction, "timestamp")), UTC)
                .astimezone(ZoneInfo(zone_name))
                .date()
            )
            if since <= at <= until:
                count += sum(
                    p.findtext("transaction_data/type") == "add"
                    and p.findtext("transaction_data/destination_team_key") == team_id
                    for p in transaction.findall("./players/player")
                )
    return count


def normalize_league(
    bundle: SyncBundle, league: InseasonLeague, identities: IdentityMappings, catalog: YahooCatalog
) -> LeagueSnapshot:
    at = datetime.fromisoformat(bundle.as_of)
    mapping = stat_mapping(bundle, catalog)
    standings = xml(bundle.documents["standings"].encode())
    on = at.astimezone(ZoneInfo(league.timezone)).date()
    current = next((w for w in league.matchups if w.start <= on <= w.end), None)
    teams: list[FantasyTeam] = []
    unresolved: list[str] = []
    mine: str | None = None
    for team in standings.findall(".//team"):
        key = text_at(team, "team_key")
        if team.findtext("is_owned_by_current_login") == "1":
            mine = key
        roster = xml(bundle.documents["roster:" + key].encode())
        players: list[str] = []
        injuries: dict[str, str] = {}
        selected_slots: dict[str, str] = {}
        for player in roster.findall(".//player"):
            external = text_at(player, "player_key")
            pid = identities.entries.get(external)
            if pid is None:
                unresolved.append(external)
                continue
            slot = player.findtext("selected_position/position")
            if slot in catalog.injury_labels:
                injuries[pid] = slot
            else:
                players.append(pid)
                if slot not in catalog.bench_labels:
                    candidate = next(
                        (
                            s
                            for s in league.starter_slots
                            if s.label == slot and s.id not in selected_slots
                        ),
                        None,
                    )
                    if candidate is None:
                        raise DataError(
                            f"Yahoo.roster.{external}: unknown or duplicate selected slot {slot}"
                        )
                    selected_slots[candidate.id] = pid
        used = (
            None
            if current is None
            else transaction_adds(bundle, key, current.start, current.end, league.timezone)
        )
        teams.append(
            FantasyTeam(
                id=key,
                name=text_at(team, "name"),
                players=tuple(players),
                injury_players=injuries,
                selected_slots=selected_slots,
                adds_used=used,
                wins=float(text_at(team, "team_standings/outcome_totals/wins")),
                losses=float(text_at(team, "team_standings/outcome_totals/losses")),
                ties=float(text_at(team, "team_standings/outcome_totals/ties")),
                seed=int(text_at(team, "team_standings/rank")),
            )
        )
    if mine is None:
        raise DataError("Yahoo.teams: current account's team was not identified")
    pairings, scores = scoreboards(bundle, mapping, at)
    free, unresolved_free = free_agents(bundle, identities)
    if len(teams) != league.teams:
        raise DataError("Yahoo.teams: team count differs from confirmed league settings")
    return LeagueSnapshot(
        format_version=1,
        league_id=league.league_id,
        as_of=at,
        settings_sha256=settings_digest(xml(bundle.documents["settings"].encode())),
        mine=mine,
        teams=tuple(teams),
        pairings=pairings,
        actual=scores,
        free_agents=free,
        unresolved_rostered=tuple(sorted(set(unresolved))),
        unresolved_free=unresolved_free,
    )


def scoreboards(
    bundle: SyncBundle, mapping: dict[str, tuple[str, ...]], at: datetime
) -> tuple[tuple[SeasonPairing, ...], tuple[ActualScore, ...]]:
    pairs: dict[tuple[str, str, str], SeasonPairing] = {}
    scores: dict[tuple[str, str], ActualScore] = {}
    for key, raw in bundle.documents.items():
        if not key.startswith("scoreboard:"):
            continue
        week = key.split(":", 1)[1]
        for matchup in xml(raw.encode()).findall(".//matchup"):
            teams = matchup.findall("./teams/team")
            if len(teams) != 2:
                continue  # A playoff bye is not a played pairing.
            home, away = sorted(text_at(t, "team_key") for t in teams)
            pairs[week, home, away] = SeasonPairing(week_id=week, home=home, away=away)
            status = text_at(matchup, "status")
            if status == "preevent":
                continue
            for team in teams:
                tid = text_at(team, "team_key")
                scores[week, tid] = ActualScore(
                    week_id=week,
                    team_id=tid,
                    through=at,
                    totals=totals(team, mapping),
                    final=status == "postevent",
                )
    return tuple(pairs[k] for k in sorted(pairs)), tuple(scores[k] for k in sorted(scores))


def free_agents(
    bundle: SyncBundle, identities: IdentityMappings
) -> tuple[tuple[FreeAgent, ...], tuple[str, ...]]:
    result: dict[str, FreeAgent] = {}
    unresolved: list[str] = []
    for key, raw in bundle.documents.items():
        if not key.startswith("players:"):
            continue
        for player in xml(raw.encode()).findall(".//player"):
            external = text_at(player, "player_key")
            pid = identities.entries.get(external)
            if pid is None:
                unresolved.append(external)
                continue
            waiver = key.startswith("players:W:")
            timestamp = player.findtext("waiver_date")
            clears = (
                datetime.fromtimestamp(int(timestamp), UTC)
                if timestamp and timestamp.isdecimal()
                else None
            )
            result[pid] = FreeAgent(
                player_id=pid, status="waiver" if waiver else "free", clears_at=clears
            )
    return tuple(result[k] for k in sorted(result)), tuple(sorted(set(unresolved)))


def league_from_draft(document: dict[str, JsonValue]) -> InseasonLeague:
    return InseasonLeague.model_validate_json(json.dumps(document))
