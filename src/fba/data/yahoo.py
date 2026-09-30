import json
from datetime import UTC, datetime
from time import monotonic
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from fba.contracts.base import DataError, Record, Text
from fba.contracts.inseason import CalculationTimeout, InseasonParameters
from fba.contracts.yahoo import DiscoveredLeague
from fba.data.codec import digest
from fba.data.storage import Store
from fba.data.yahoo_auth import YahooReader


def xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise DataError("Yahoo XML: DTD and entity declarations are prohibited")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise DataError("Yahoo XML: malformed response") from None
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag == "error" or root.find(".//error") is not None:
        raise DataError("Yahoo XML: provider returned an error document")
    return root


def text_at(node: ET.Element, path: str) -> str:
    value = node.findtext(path)
    if value is None or not value.strip():
        raise DataError(f"Yahoo.{path}: missing required value")
    return value.strip()


def xml_value(node: ET.Element) -> JsonValue:
    if len(node) == 0:
        return node.text.strip() if node.text else ""
    result: dict[str, JsonValue] = {}
    for child in node:
        value = xml_value(child)
        if child.tag in result:
            current = result[child.tag]
            result[child.tag] = [*current, value] if isinstance(current, list) else [current, value]
        else:
            result[child.tag] = value
    return result


def settings_digest(root: ET.Element) -> str:
    settings = root.find(".//settings")
    if settings is None:
        raise DataError("Yahoo.settings: missing settings")
    return digest(json.dumps(xml_value(settings), sort_keys=True, separators=(",", ":")).encode())


def discover(reader: YahooReader) -> tuple[DiscoveredLeague, ...]:
    root = xml(reader.get("users;use_login=1/games;game_codes=nba/leagues"))
    rows: list[DiscoveredLeague] = []
    for game in root.findall(".//game"):
        season = text_at(game, "season")
        for league in game.findall(".//league"):
            rows.append(
                DiscoveredLeague(
                    key=text_at(league, "league_key"),
                    game_key=text_at(game, "game_key"),
                    season=season,
                    name=text_at(league, "name"),
                )
            )
    if not rows:
        raise DataError("Yahoo: this account has no accessible NBA leagues")
    current = max(r.season for r in rows)
    return tuple(sorted((r for r in rows if r.season == current), key=lambda r: r.key))


class SyncBundle(Record):
    league_key: Text
    as_of: str
    documents: dict[str, str]
    snapshot_hashes: dict[str, str]
    requests: int
    elapsed_seconds: float


class YahooSync:
    def __init__(self, reader: YahooReader, store: Store, params: InseasonParameters) -> None:
        self.reader, self.store, self.params = reader, store, params
        self.documents: dict[str, str] = {}
        self.hashes: dict[str, str] = {}
        self.started = 0.0
        self.initial_requests = 0

    def fetch(self, label: str, resource: str, at: datetime) -> ET.Element:
        if self.reader.requests - self.initial_requests >= self.params.maximum_requests.value:
            raise DataError("Yahoo.sync: request budget exhausted")
        if monotonic() - self.started > self.params.budgets["sync"].value:
            raise CalculationTimeout("Yahoo.sync: configured synchronization time budget exceeded")
        start, requests = monotonic(), self.reader.requests
        data = self.reader.get(resource)
        root = xml(data)
        raw = data.decode("utf-8")
        sha = self.store.snapshot("Yahoo:" + resource, at, raw)
        self.documents[label], self.hashes[label] = raw, sha
        self.store.append_snapshot(
            "sync-log",
            "Yahoo sync",
            at,
            {
                "dataset": label,
                "rows": row_count(label, root),
                "requests": self.reader.requests - requests,
                "elapsed_seconds": monotonic() - start,
                "sha256": sha,
                "result": "success",
            },
        )
        return root

    def pages(self, label: str, resource: str, tag: str, at: datetime) -> None:
        start = 0
        while True:
            root = self.fetch(f"{label}:{start}", f"{resource};start={start};count=25", at)
            count = len(root.findall(".//" + tag))
            if count < 25:
                return
            start += count

    def sync(self, league: DiscoveredLeague) -> SyncBundle:
        self.started, self.initial_requests = monotonic(), self.reader.requests
        self.reader.request_limit = self.initial_requests + self.params.maximum_requests.value
        self.reader.deadline = self.started + self.params.budgets["sync"].value
        self.documents, self.hashes = {}, {}
        now = datetime.now(UTC)
        prefix = "league/" + league.key
        try:
            self.fetch("settings", prefix + "/settings", now)
            self.fetch("stat-catalog", "game/" + league.game_key + "/stat_categories", now)
            weeks = self.fetch("weeks", "game/" + league.game_key + "/game_weeks", now)
            teams = self.fetch("standings", prefix + "/standings", now)
            for team in teams.findall(".//team"):
                key = text_at(team, "team_key")
                self.fetch("roster:" + key, "team/" + key + "/roster/players", now)
            for week in weeks.findall(".//game_week"):
                key = text_at(week, "week")
                self.fetch("scoreboard:" + key, prefix + "/scoreboard;week=" + key, now)
            for status in ("FA", "W"):
                self.pages("players:" + status, prefix + "/players;status=" + status, "player", now)
            self.pages("transactions", prefix + "/transactions", "transaction", now)
        except (DataError, CalculationTimeout, ValueError) as exc:
            self.store.append_snapshot(
                "sync-log",
                "Yahoo sync",
                now,
                {
                    "result": "failed",
                    "error": str(exc),
                    "requests": self.reader.requests - self.initial_requests,
                    "elapsed_seconds": monotonic() - self.started,
                },
            )
            raise
        finally:
            self.reader.request_limit = None
            self.reader.deadline = None
        return SyncBundle(
            league_key=league.key,
            as_of=now.isoformat(),
            documents=self.documents,
            snapshot_hashes=self.hashes,
            requests=self.reader.requests - self.initial_requests,
            elapsed_seconds=monotonic() - self.started,
        )


def row_count(label: str, root: ET.Element) -> int:
    tags = {
        "settings": "settings",
        "stat-catalog": "stat",
        "weeks": "game_week",
        "standings": "team",
        "roster": "player",
        "scoreboard": "matchup",
        "players": "player",
        "transactions": "transaction",
    }
    return len(root.findall(".//" + tags[label.split(":", 1)[0]]))
