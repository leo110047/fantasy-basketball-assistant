import json
from datetime import UTC, datetime, timedelta

import pytest
from inseason_support import DEFAULTS, fixture, parameters
from pydantic import SecretStr

from fba.contracts.base import DataError
from fba.contracts.inseason import AuthorizationRequired, InseasonPreferences, SyncState
from fba.core.inseason import availability
from fba.data.codec import canonical
from fba.data.storage import Store, atomic_write
from fba.data.yahoo import xml
from fba.data.yahoo_auth import Credentials, HttpResult, YahooAuth, YahooReader
from fba.runtime.local import InstanceLock, LocalServer, permitted


class Vault:
    def __init__(self, value=None):
        self.value = value

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


def credential(expired=False):
    return Credentials(
        client_id=SecretStr("test-id"),
        client_secret=SecretStr("synthetic-secret"),
        access_token=SecretStr("synthetic-old-access"),
        refresh_token=SecretStr("synthetic-refresh"),
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    )


def token_reply():
    return HttpResult(
        status=200,
        body=json.dumps(
            {
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-rotated",
                "expires_in": 3600,
                "token_type": "bearer",
            }
        ).encode(),
    )


def test_expiry_refresh_rotation_and_read_only_api():
    calls = []
    vault = Vault(credential(expired=True))

    def transport(url, method, headers, body, timeout):
        calls.append((url, method, headers, body))
        return token_reply() if method == "POST" else HttpResult(status=200, body=b"<ok/>")

    auth = YahooAuth(vault, parameters(), transport)
    assert YahooReader(auth).get("league/test/settings") == b"<ok/>"
    assert vault.value.refresh_token.get_secret_value() == "synthetic-rotated"
    assert calls[0][0] == "https://api.login.yahoo.com/oauth2/get_token"
    assert calls[1][1] == "GET"
    assert calls[1][2]["Authorization"] == "Bearer synthetic-new-access"
    assert b"synthetic-secret" not in canonical(vault.value)


def test_401_refresh_has_independent_retry_even_with_zero_network_retries():
    params = parameters()
    params = params.model_copy(
        update={"retry_count": params.retry_count.model_copy(update={"value": 0})}
    )
    statuses = iter((401, 200))
    calls = []

    def transport(url, method, headers, body, timeout):
        calls.append(method)
        return token_reply() if method == "POST" else HttpResult(status=next(statuses), body=b"ok")

    assert YahooReader(YahooAuth(Vault(credential()), params, transport)).get("users") == b"ok"
    assert calls == ["GET", "POST", "GET"]


def test_invalid_refresh_is_explicit_and_does_not_replace_vault():
    vault = Vault(credential(expired=True))
    before = vault.value
    auth = YahooAuth(vault, parameters(), lambda *args: HttpResult(status=401, body=b"secret"))
    with pytest.raises(AuthorizationRequired, match="需要重新連結") as error:
        YahooReader(auth).get("users")
    assert "secret" not in str(error.value)
    assert vault.value == before


def test_rate_limit_exponential_backoff_and_failure():
    waits = []
    params = parameters()
    auth = YahooAuth(
        Vault(credential()), params, lambda *args: HttpResult(status=429, body=b""), waits.append
    )
    reader = YahooReader(auth)
    with pytest.raises(DataError, match="retry budget exhausted"):
        reader.get("users")
    assert reader.requests == params.retry_count.value + 1
    assert waits == [params.retry_seconds.value * 2**i for i in range(params.retry_count.value)]


@pytest.mark.parametrize(
    "resource", ["https://evil.test", "//evil.test", "users?token=x", "../secret"]
)
def test_yahoo_paths_cannot_redirect_credentials(resource):
    auth = YahooAuth(Vault(credential()), parameters(), lambda *args: pytest.fail("network called"))
    with pytest.raises(DataError, match="invalid path"):
        YahooReader(auth).get(resource)


def test_xml_entity_rejection():
    with pytest.raises(DataError, match="DTD"):
        xml(b'<!DOCTYPE x [<!ENTITY z SYSTEM "file:///tmp/secret">]><x>&z;</x>')


def test_immutable_history_and_tamper_detection(tmp_path):
    store = Store(tmp_path / "中文 資料")
    at = datetime.now(UTC)
    first = store.append_snapshot("predictions", "test", at, {"probability": 0.75})
    store.append_snapshot("predictions", "test", at, {"probability": 0.2})
    assert store.history("predictions")[0].payload == {"probability": 0.75}
    path = store.root / "snapshots" / f"{first}.json"
    path.write_bytes(path.read_bytes().replace(b"0.75", b"0.25"))
    with pytest.raises(DataError, match="hash mismatch"):
        store.load_snapshot(first)


def test_atomic_write_interruption_preserves_previous_complete_document(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    atomic_write(path, b'{"old":true}')

    def interrupted(*args):
        raise OSError("simulated interruption before publication")

    monkeypatch.setattr("fba.data.storage.os.replace", interrupted)
    with pytest.raises(OSError):
        atomic_write(path, b'{"new":true}')
    assert json.loads(path.read_bytes()) == {"old": True}
    assert not list(tmp_path.glob(".pending-*"))


@pytest.mark.parametrize(
    "change",
    [
        {"connected": False},
        {"authorization_valid": False},
        {"last_success": datetime(2000, 1, 1, tzinfo=UTC)},
        {"settings_pending": True},
        {"unresolved_rostered": ("unmapped",)},
    ],
)
def test_all_league_features_share_explicit_disable_and_recovery(change):
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    now = datetime.now(UTC)
    good = SyncState(
        connected=True,
        authorization_valid=True,
        last_success=now,
        last_error=None,
        settings_pending=False,
        unresolved_rostered=(),
    )
    blocked = availability(good.model_copy(update=change), prefs, now, players_as_of=now)
    assert not blocked.enabled and blocked.reason and blocked.repair
    assert availability(good, prefs, now, players_as_of=now).enabled


def test_mutations_require_origin_host_and_token():
    valid = {
        "Host": "127.0.0.1:8900",
        "Origin": "http://127.0.0.1:8900",
        "Authorization": "Bearer test",
    }
    assert permitted(valid, 8900, "test", authenticated=True, mutation=True)
    for key in valid:
        assert not permitted(
            {k: v for k, v in valid.items() if k != key},
            8900,
            "test",
            authenticated=True,
            mutation=True,
        )
    for key, value in (
        ("Host", "evil.test:8900"),
        ("Origin", "http://evil.test"),
        ("Authorization", "Bearer wrong"),
    ):
        assert not permitted({**valid, key: value}, 8900, "test", authenticated=True, mutation=True)


def test_busy_port_fallback_lock_release_and_immediate_rebind(tmp_path):
    from http.server import BaseHTTPRequestHandler

    with LocalServer(0, BaseHTTPRequestHandler) as first:
        with LocalServer(first.server_port, BaseHTTPRequestHandler, 2) as second:
            assert first.server_port != second.server_port
            assert second.server_address[0] == "127.0.0.1"
    lock, second_lock = InstanceLock(tmp_path / "中文 lock"), InstanceLock(tmp_path / "中文 lock")
    assert lock.acquire()
    assert not second_lock.acquire()
    lock.release()
    assert second_lock.acquire()
    second_lock.release()
    with LocalServer(first.server_port, BaseHTTPRequestHandler) as rebound:
        assert rebound.server_port == first.server_port


def test_previous_season_does_not_enter_current_blend():
    from fba.inseason.projection import effective_projection

    league, params, players, priors, ledger, _, now = fixture()
    before = effective_projection(players, priors, ledger, league, params, now, now.date())
    previous = players.boxes[0].model_copy(
        update={
            "game_id": "previous-season",
            "played_at": now - timedelta(days=365),
            "known_at": now - timedelta(days=364),
            "minutes": 5000.0,
        }
    )
    after = effective_projection(
        players.model_copy(update={"boxes": (*players.boxes, previous)}),
        priors,
        ledger,
        league,
        params,
        now,
        now.date(),
    )
    assert before == after


@pytest.mark.parametrize("steals_label", ["ST", "STL"])
@pytest.mark.parametrize("at", ["2026-10-13T08:00:00+00:00", "2026-10-15T01:00:00+00:00"])
def test_yahoo_xml_normalization_covers_settings_rosters_scores_and_unmapped_free_agents(
    at, steals_label
):
    from pathlib import Path

    from fba.contracts.yahoo import DiscoveredLeague, IdentityMappings, YahooCatalog
    from fba.data.yahoo import SyncBundle
    from fba.data.yahoo_normalize import league_from_draft, normalize_league, settings_draft

    raw = json.loads((Path(__file__).parent / "fixtures/yahoo-example.json").read_bytes())
    assert "not a recorded Yahoo response" in raw["kind"]
    raw["documents"]["stat-catalog"] = raw["documents"]["stat-catalog"].replace(
        "<display_name>STL</display_name>", f"<display_name>{steals_label}</display_name>"
    )
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    bundle = SyncBundle(
        league_key="fixture.l.1",
        as_of=at,
        documents=raw["documents"],
        snapshot_hashes={},
        requests=12,
        elapsed_seconds=1.0,
    )
    selected = DiscoveredLeague(
        key="fixture.l.1", game_key="fixture", season="2026", name="Synthetic"
    )
    draft = settings_draft(bundle, selected, catalog)
    league = league_from_draft(draft.document).model_copy(
        update={"timezone": "America/New_York", "adds_per_week": 1}
    )
    result = normalize_league(
        bundle,
        league,
        IdentityMappings(entries={f"external{i}": f"p{i}" for i in range(7)}),
        catalog,
    )
    assert result.mine == "team0"
    assert result.teams[0].adds_used == league.adds_per_week
    assert result.unresolved_rostered == () and result.unresolved_free == ("unmapped-free",)
    assert result.teams[0].selected_slots == {"PG:0": "p0", "C:0": "p1"}
    assert len(result.pairings) == 3 and len(result.actual) == 4
    assert result.actual[0].totals["FGM"] == 1.0
    assert result.actual[0].totals["STL"] == 1.0
    assert any(c.id == "STL" for c in league.categories)
    assert result.free_agents[0].player_id == "p6"
