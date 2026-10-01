"""Synthetic provider contracts; no claim of live Yahoo authentication/coverage."""

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from inseason_support import fixture

from fba.contracts.base import DataError
from fba.contracts.yahoo import DiscoveredLeague, IdentityMappings
from fba.data.storage import Store
from fba.data.yahoo import SyncBundle, YahooSync
from fba.data.yahoo_identity import resolve_identities, unresolved_metadata
from fba.data.yahoo_normalize import ownership


def bundle(raw):
    return SyncBundle(
        league_key="fixture",
        as_of="2026-09-30T12:00:00Z",
        documents={"players:FA:0": raw},
        snapshot_hashes={},
        requests=1,
        elapsed_seconds=0.0,
    )


def test_owned_zero_unknown_delta_and_time_are_distinct():
    raw = """<players><player><player_key>a</player_key><percent_owned><value>0</value>
      <delta>-2.5</delta><coverage_type>season</coverage_type></percent_owned></player>
      <player><player_key>b</player_key></player></players>"""
    at = datetime(2026, 9, 30, 12, tzinfo=UTC)
    rows = ownership(bundle(raw), IdentityMappings(entries={"a": "p0", "b": "p1"}), at)
    assert rows["p0"].value == 0 and rows["p0"].change == -0.025
    assert rows["p1"].value is None and rows["p1"].change is None
    assert rows["p0"].as_of == rows["p1"].as_of == at
    with pytest.raises(DataError, match="invalid percentage"):
        ownership(
            bundle(raw.replace("<value>0", "<value>101")), IdentityMappings(entries={"a": "p0"}), at
        )


def player_xml(key, name, editorial="nba.p.stable", team="BOS"):
    return f"""<players><player><player_key>{key}</player_key><name><full>{name}</full></name>
      <editorial_player_key>{editorial}</editorial_player_key><editorial_team_abbr>{team}</editorial_team_abbr>
      <eligible_positions><position>PG</position></eligible_positions></player></players>"""


def test_unique_name_team_position_match_survives_season_key_change():
    players = fixture()[2]
    p0 = players.players[0].model_copy(update={"team_abbreviation": "BOS"})
    players = players.model_copy(update={"players": (p0, *players.players[1:])})
    first = resolve_identities(
        bundle(player_xml("old.p.1", "Player 0")), players, IdentityMappings(entries={})
    )
    assert first.entries["old.p.1"] == "p0" and first.stable_entries["nba.p.stable"] == "p0"
    second = resolve_identities(
        bundle(player_xml("new.p.1", "New Display Name", team="LAL")), players, first
    )
    assert second.entries["new.p.1"] == "p0"
    assert (
        unresolved_metadata(bundle(player_xml("old.p.1", "Player 0")))["old.p.1"]["team"] == "BOS"
    )


def test_ambiguous_name_match_and_missing_reliable_metadata_stay_unresolved():
    players = fixture()[2]
    twins = tuple(
        p.model_copy(update={"name": "Same Name", "team_abbreviation": "BOS", "positions": ("PG",)})
        for p in players.players[:2]
    )
    players = players.model_copy(update={"players": (*twins, *players.players[2:])})
    result = resolve_identities(
        bundle(player_xml("new", "Same Name")), players, IdentityMappings(entries={})
    )
    assert result.entries == {} and result.stable_entries == {}
    result = resolve_identities(
        bundle(player_xml("new", "Player 2")), players, IdentityMappings(entries={})
    )
    assert result.entries == {}


class Reader:
    def __init__(self, now):
        self.now, self.requests, self.calls = now, 0, []
        self.deadline = self.request_limit = None
        start = now.date() - timedelta(days=24 * 7 + 2)
        self.weeks = (
            "<game_weeks>"
            + "".join(
                f"<game_week><week>{i + 1}</week>"
                f"<start>{start + timedelta(days=7 * i)}</start>"
                f"<end>{start + timedelta(days=7 * i + 6)}</end>"
                "</game_week>"
                for i in range(33)
            )
            + "</game_weeks>"
        )

    def stamp(self, i):
        return int((self.now - timedelta(hours=i if i < 40 else 10000 + i)).timestamp())

    def get(self, resource):
        self.requests += 1
        self.calls.append(resource)
        if resource.endswith("/settings"):
            return (
                b"<league><settings><playoff_start_week>30</playoff_start_week></settings></league>"
            )
        if resource.endswith("/stat_categories"):
            return b"<stats/>"
        if resource.endswith("/game_weeks"):
            return self.weeks.encode()
        if resource.endswith("/standings"):
            return (
                "<teams>"
                + "".join(f"<team><team_key>team{i}</team_key></team>" for i in range(14))
                + "</teams>"
            ).encode()
        if "/roster/" in resource:
            return b"<players/>"
        if "/scoreboard;" in resource:
            return b"<scoreboard/>"
        start = int(resource.split(";start=")[1].split(";")[0])
        if "/transactions;" in resource:
            return (
                "<transactions>"
                + "".join(
                    f"<transaction><transaction_key>t{i}</transaction_key>"
                    f"<timestamp>{self.stamp(i)}"
                    "</timestamp>"
                    "</transaction>"
                    for i in range(start, min(start + 25, 5000))
                )
                + "</transactions>"
            ).encode()
        count = 400 if ";status=FA;" in resource else 0
        return (
            "<players>"
            + "".join(
                f"<player><player_key>p{i}</player_key></player>"
                for i in range(start, min(start + 25, count))
            )
            + "</players>"
        ).encode()


def test_long_season_sync_is_bounded_and_retains_original_replies(tmp_path, monkeypatch):
    import fba.data.yahoo as module

    now = datetime(2026, 9, 30, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(module, "datetime", Clock)
    reader = Reader(now)
    store = Store(tmp_path)
    params = fixture()[1]
    selected = DiscoveredLeague(key="fixture", game_key="nba", season="2026", name="Synthetic")
    first = YahooSync(reader, store, params).sync(selected, timezone="America/New_York")
    assert first.requests == 71 and first.requests <= 100
    original = first.snapshot_hashes["scoreboard:1"]
    before = len(reader.calls)
    second = YahooSync(reader, store, params).sync(selected, first, timezone="America/New_York")
    calls = reader.calls[before:]
    assert second.requests == 46 and second.requests <= 100
    assert sum("/scoreboard;" in call for call in calls) == 8
    assert sum("/transactions;" in call for call in calls) == 2
    assert all("percent_owned" in call for call in calls if "/players" in call)
    assert second.refreshed_at["scoreboard:1"] == first.refreshed_at["scoreboard:1"]
    assert store.load_snapshot(original).payload == first.documents["scoreboard:1"]
    assert (
        len(ET.fromstring(second.documents["transactions:retained"]).findall("transaction")) == 50
    )
