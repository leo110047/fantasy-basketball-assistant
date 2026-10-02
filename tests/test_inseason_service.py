import json
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection

import pytest
from inseason_support import DEFAULTS, fixture
from test_inseason_data import Vault

from fba.apps.inseason.api import Jobs
from fba.apps.inseason.server import SeasonServer
from fba.contracts.base import DataError
from fba.contracts.inseason import SyncState
from fba.contracts.inseason_app import ConfirmSettingsRequest, LeagueState
from fba.contracts.inseason_results import TradeSearchResult
from fba.contracts.yahoo import DiscoveredLeague, LeagueDraft
from fba.data.codec import canonical, digest
from fba.inseason.session import InseasonSession


def selected_session(tmp_path):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    league, *_ = fixture()
    selected = DiscoveredLeague(
        key=league.league_id, game_key=league.game_key, name=league.name, season=league.season_id
    )
    session.discovered = (selected,)
    session.select(selected.key)
    document = league.model_dump(mode="json")
    raw = {"max_weekly_adds": str(league.adds_per_week)}
    state = session.state().model_copy(
        update={
            "league": league,
            "draft": LeagueDraft(document=document, yahoo_settings=raw, confirm_fields=()),
            "confirmed_yahoo_settings": raw,
            "confirmed_local_sha256": digest(canonical(league)),
        }
    )
    session.save_state(state)
    return session


def test_confirmation_remembers_local_choice_and_detects_later_edits(tmp_path):
    session = selected_session(tmp_path)
    state = session.state()
    assert (
        session.setting_differences(state, state.draft.document, state.draft.yahoo_settings) == []
    )
    remote = {**state.draft.yahoo_settings, "max_weekly_adds": "10"}
    assert session.setting_differences(state, state.draft.document, remote)
    accepted = state.model_copy(update={"confirmed_yahoo_settings": remote})
    assert session.setting_differences(accepted, state.draft.document, remote) == []
    changed = accepted.model_copy(
        update={"league": state.league.model_copy(update={"adds_per_week": 20})}
    )
    assert session.setting_differences(changed, state.draft.document, remote)


def test_other_league_identity_cannot_be_confirmed(tmp_path):
    session = selected_session(tmp_path)
    state = session.state().model_copy(update={"bundle_sha256": "f" * 64})
    session.save_state(state)
    document = {**state.draft.document, "league_id": "another-account-league"}
    with pytest.raises(DataError, match="identity"):
        session.confirm_settings(ConfirmSettingsRequest(document=document, decision="import"))


def test_missing_player_sources_do_not_break_settings_repair_page(tmp_path):
    session = selected_session(tmp_path)
    result = Jobs(session).bootstrap()
    assert result["state"]["selected"]["key"] == session.preferences.selected_league
    assert "projection" not in result


def test_bootstrap_exposes_sync_times_and_safe_credential_status(tmp_path):
    from test_inseason_data import credential

    session = selected_session(tmp_path)
    session.auth.vault.save(credential().model_copy(update={"account_id": "synthetic-account"}))
    at = datetime.now(UTC)
    session.next_sync_at = at
    session.league_store().append_snapshot("sync-log", "test", at, {"result": "success"})
    result = Jobs(session).bootstrap()
    assert result["sync_log"][0]["at"] == at.isoformat()
    assert result["next_sync_at"] == at.isoformat()
    assert result["credentials"]["account_id"] == "synthetic-account"
    encoded = json.dumps(result)
    for secret in ("synthetic-secret", "synthetic-old-access", "synthetic-refresh"):
        assert secret not in encoded


@pytest.mark.parametrize("status,completed", [("completed", 4), ("cancelled", 1)])
@pytest.mark.parametrize("latest_action", ["trade", "partners"])
def test_bootstrap_restores_latest_saved_search_independently_of_job(
    tmp_path, monkeypatch, status, completed, latest_action
):
    session = selected_session(tmp_path)
    jobs = Jobs(session)
    assert jobs.bootstrap()["last_trade_search"] is None
    at = datetime.now(UTC)
    result = TradeSearchResult(
        trades=(),
        counts={"eligible": 4, "bounded": completed, "full_effects": 0},
        minimum_value_ratio=0.7,
        status=status,
        completed=completed,
    ).model_dump(mode="json")
    store = session.league_store()
    store.append_snapshot("trade-searches", "previous search", at - timedelta(days=1), {})
    store.append_snapshot("trade-searches", "trade search", at, result)
    # Other league history must never leak into this league's bootstrap.
    session.league_store("another-league").append_snapshot(
        "trade-searches", "another league", at + timedelta(days=1), {}
    )
    monkeypatch.setattr("fba.apps.inseason.api.basic_action", lambda *_: {"detail": True})
    try:
        jobs.start(latest_action, b"{}")
        jobs.thread.join(timeout=3)
        assert jobs.read()["status"] == "completed"
        assert jobs.read()["action"] == latest_action
        assert jobs.bootstrap()["last_trade_search"] == {
            "saved_at": at.isoformat(),
            "result": result,
        }
        # A fresh process must recover from disk, without running a calculation.
        restarted = InseasonSession(tmp_path, DEFAULTS, Vault())
        assert (
            Jobs(restarted).bootstrap()["last_trade_search"]
            == jobs.bootstrap()["last_trade_search"]
        )
    finally:
        jobs.stop(3)


def test_http_security_and_job_errors_are_visible_and_secret_free(tmp_path):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    session.preferences = session.preferences.model_copy(update={"preferred_port": 0})
    with SeasonServer(session) as server:
        thread = threading.Thread(target=server.run)
        thread.start()
        try:

            def request(path, method="GET", payload=None, headers=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                body = json.dumps(payload) if payload is not None else None
                merged = {
                    "Authorization": "Bearer " + server.token,
                    "Content-Type": "application/json",
                    "Origin": server.origin,
                }
                merged.update(headers or {})
                conn.request(method, path, body=body, headers=merged)
                response = conn.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                conn.close()
                return result

            status, headers, body = request("/api/bootstrap")
            assert status == 200 and not json.loads(body)["availability"]["enabled"]
            assert not any(h.lower().startswith("access-control-") for h in headers)
            status, headers, body = request("/?view=today")
            assert status == 200 and b'id="content"' in body
            assert headers["Content-Type"].startswith("text/html")
            status, headers, body = request("/court.jpg")
            assert status == 200 and body.startswith(b"\xff\xd8\xff")
            assert headers["Content-Type"].startswith("image/jpeg")
            assert request("/../server.py")[0] == 404
            assert request("/api/bootstrap", headers={"Host": "evil.test"})[0] == 403
            assert request("/api/quit", "POST", {}, {"Authorization": "Bearer wrong"})[0] == 403
            assert request("/api/quit", "POST", {}, {"Origin": "null"})[0] == 403
            assert (
                request(
                    "/api/action",
                    "POST",
                    {
                        "action": "connect",
                        "payload": {
                            "client_id": "synthetic-id",
                            "client_secret": "synthetic-secret",
                            "unexpected": "secret-value",
                        },
                    },
                )[0]
                == 202
            )
            server.jobs.thread.join(timeout=3)
            result = server.jobs.read()
            assert result["status"] == "failed" and "unexpected" in result["error"]
            assert "secret-value" not in result["error"]
            assert "synthetic-secret" not in result["error"]
            for path in tmp_path.rglob("*.json"):
                assert b"synthetic-secret" not in path.read_bytes()
                assert b"secret-value" not in path.read_bytes()
            assert request("/api/quit", "POST", {})[0] == 200
        finally:
            server.stopping.set()
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_synchronize_failure_preserves_previous_success_and_explicit_error(tmp_path, monkeypatch):
    session = selected_session(tmp_path)
    now = datetime.now(UTC)
    state = session.state().model_copy(
        update={
            "sync": SyncState(
                connected=True,
                authorization_valid=True,
                last_success=now,
                settings_pending=False,
                last_error=None,
                unresolved_rostered=(),
            )
        }
    )
    session.save_state(state)

    def failed(*args, **kwargs):
        raise DataError("Yahoo: rate limit retry budget exhausted")

    monkeypatch.setattr("fba.inseason.session.YahooSync.sync", failed)
    with pytest.raises(DataError, match="rate limit"):
        session.synchronize()
    after = session.state()
    assert isinstance(after, LeagueState)
    assert after.sync.last_success == now and after.sync.last_error


def test_never_linked_player_workspace_supports_projection_adjustment_and_undo(tmp_path):
    from fba.contracts.inseason import ProjectionRules
    from fba.contracts.inseason_app import (
        AdjustmentChange,
        AdjustmentsRequest,
        PlayerWorkspace,
        RevokeRequest,
        Sources,
    )
    from fba.inseason.operations import bootstrap, revoke_group, save_adjustments

    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    league, _, players, priors, *_ = fixture()
    store = session.working_store()
    rules = ProjectionRules.model_validate(
        {k: getattr(league, k) for k in ProjectionRules.model_fields}
    )
    at = datetime.now(UTC)
    player_sha = store.snapshot("synthetic-player-feed", at, players.model_dump(mode="json"))
    prior_sha = store.snapshot("synthetic-preseason", at, priors.model_dump(mode="json"))
    store.write(
        "workspace.json",
        PlayerWorkspace(
            sources=Sources(
                forecast_path="fixture",
                forecast_sha256="a" * 64,
                forecast_known_at=priors.known_at,
                player_url="https://example.invalid/feed",
                permission_reference="synthetic fixture only",
            ),
            rules=rules,
            players_sha256=player_sha,
            priors_sha256=prior_sha,
        ),
    )
    before = session.player_projection(at)
    data = bootstrap(session)
    assert not data["availability"]["enabled"] and data["projection"]
    assert data["state"] is None and "snapshot" not in data
    request = AdjustmentsRequest(
        expected_sha256=digest(canonical(session.ledger())),
        changes=(
            AdjustmentChange(
                player_id="p0",
                field="minutes",
                value=37.0,
                starts_on=before.on,
                ends_on=before.on,
                reason="offline player adjustment",
                replaces=None,
            ),
        ),
        preview=False,
    )
    save_adjustments(session, request)
    assert session.player_projection(datetime.now(UTC)).players[0].minutes == 37.0
    ledger = session.ledger()
    revoke_group(
        session,
        RevokeRequest(
            expected_sha256=digest(canonical(ledger)), group_id=ledger.entries[0].group_id
        ),
    )
    assert (
        session.player_projection(datetime.now(UTC)).players[0].minutes == before.players[0].minutes
    )
    assert len(session.ledger().entries) == 2
    assert not (tmp_path / "leagues").exists()


@pytest.mark.parametrize("changed", ["snapshot", "ledger", "parameters"])
def test_saved_plan_requires_current_snapshot_ledger_and_parameters(tmp_path, changed):
    from inseason_support import simulation
    from test_inseason_review import prediction

    from fba.contracts.inseason import AdjustmentEntry, AdjustmentLedger
    from fba.contracts.inseason_results import AddPlan
    from fba.inseason.operations import recorded_plan
    from fba.inseason.session import json_value

    session = selected_session(tmp_path)
    sim = simulation()
    session.params = sim.params
    state = session.state().model_copy(update={"normalized_sha256": "a" * 64})
    session.save_state(state)
    record = prediction(sim)
    plan = AddPlan(
        id="saved-plan",
        moves=(),
        before=record.with_adjustments,
        after=record.with_adjustments,
        delta_week=0,
        delta_season=0,
        score=0,
        traces=(),
    )
    record = record.model_copy(update={"recommendations": (plan,)})
    session.league_store().append_snapshot(
        "predictions", "test", sim.as_of, json_value(record.model_dump(mode="json"))
    )
    assert recorded_plan(session, plan.id) == plan
    if changed == "snapshot":
        session.save_state(state.model_copy(update={"normalized_sha256": "b" * 64}))
    elif changed == "parameters":
        session.params = sim.params.model_copy(update={"version": "updated-parameters"})
    else:
        session.league_store().write(
            "ledger.json",
            AdjustmentLedger(
                format_version=1,
                entries=(
                    AdjustmentEntry(
                        id="change",
                        group_id="change",
                        player_id="p0",
                        team_id="nba0",
                        field="minutes",
                        value=31.0,
                        starts_on=sim.as_of.date(),
                        ends_on=sim.as_of.date(),
                        reason="updated minutes",
                        created_at=sim.as_of,
                        replaces=None,
                        revokes=(),
                    ),
                ),
            ),
        )
    with pytest.raises(DataError, match="重新計算 F3"):
        recorded_plan(session, plan.id)


def test_new_yahoo_labels_reach_existing_catalog_without_overwriting_user_mapping(tmp_path):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    old = session.catalog.model_copy(
        update={
            "category_labels": {
                key: value for key, value in session.catalog.category_labels.items() if key != "ST"
            },
            "stat_labels": {
                key: value for key, value in session.catalog.stat_labels.items() if key != "ST"
            },
        }
    )
    session.store.write("yahoo-catalog.json", old)
    original = (tmp_path / "yahoo-catalog.json").read_bytes()
    updated = InseasonSession(tmp_path, DEFAULTS, Vault())
    assert updated.catalog.category_labels["ST"].id == "STL"
    assert updated.catalog.stat_labels["ST"] == ("STL",)
    assert (tmp_path / "yahoo-catalog.json").read_bytes() == original
    custom = old.model_copy(update={"stat_labels": {**old.stat_labels, "ST": ("CUSTOM",)}})
    session.store.write("yahoo-catalog.json", custom)
    assert InseasonSession(tmp_path, DEFAULTS, Vault()).catalog.stat_labels["ST"] == ("CUSTOM",)


def test_cancelled_search_persists_partial_results_then_allows_another_job(tmp_path, monkeypatch):
    from test_inseason_trade_workers import ranked_case

    from fba.apps.inseason.trade_workers import TradeWorkers

    session = selected_session(tmp_path)
    monkeypatch.setattr(session, "simulation", lambda **_: ranked_case("h2h_one_win"))
    jobs = Jobs(session)
    finished = TradeWorkers.completed_candidate
    cancelled = False

    def cancel_after_first(owner, trade):
        nonlocal cancelled
        finished(owner, trade)
        if not cancelled:
            cancelled = True
            jobs.cancel(jobs.identifier)

    monkeypatch.setattr(TradeWorkers, "completed_candidate", cancel_after_first)
    try:
        jobs.start("trade-search", b'{"opponent":null,"size":1}')
        jobs.thread.join(timeout=20)
        assert not jobs.thread.is_alive()
        result = jobs.read()
        assert result["status"] == "cancelled"
        assert result["error"] is None
        assert result["result"]["status"] == "cancelled"
        assert result["result"]["completed"] == 1
        assert len(result["result"]["trades"]) == 1
        stored = session.league_store().history("trade-searches")[-1].payload
        assert stored == result["result"]
        assert jobs.bootstrap()["last_trade_search"]["result"] == result["result"]
        assert not result["can_cancel"]
        jobs.start("trade-search", b'{"opponent":null,"size":1}')
        jobs.thread.join(timeout=20)
        assert not jobs.thread.is_alive()
        result = jobs.read()
        assert result["status"] == "completed"
        assert result["result"]["completed"] == result["search"]["total"]
        assert not session.cancelled.is_set()
    finally:
        jobs.stop(3)


def test_cancel_endpoint_requires_authority_and_current_job_and_keeps_busy_until_drained(
    tmp_path, monkeypatch
):
    session = InseasonSession(tmp_path, DEFAULTS, Vault())
    session.preferences = session.preferences.model_copy(update={"preferred_port": 0})
    started, release = threading.Event(), threading.Event()

    def search(*_):
        started.set()
        assert release.wait(5)
        return {"status": "cancelled"}

    monkeypatch.setattr("fba.apps.inseason.api.basic_action", search)
    with SeasonServer(session) as server:
        thread = threading.Thread(target=server.run)
        thread.start()

        def cancel(identifier, headers=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request(
                "POST",
                "/api/cancel",
                json.dumps({"job": identifier}),
                {
                    "Authorization": "Bearer " + server.token,
                    "Origin": server.origin,
                    "Content-Type": "application/json",
                    **(headers or {}),
                },
            )
            response = conn.getresponse()
            status = response.status
            response.read()
            conn.close()
            return status

        try:
            identifier = server.jobs.start("trade-search", b"{}")
            assert started.wait(3)
            assert cancel(identifier, {"Authorization": "Bearer wrong"}) == 403
            assert cancel(identifier, {"Origin": "null"}) == 403
            assert cancel(identifier + 1) == 400
            assert cancel(True) == 400
            assert not session.cancelled.is_set()
            assert cancel(identifier) == 202
            assert cancel(identifier) == 202
            assert server.jobs.read()["status"] == "cancelling"
            with pytest.raises(DataError, match="目前仍在執行"):
                server.jobs.start("week", b"{}")
            release.set()
            server.jobs.thread.join(timeout=3)
            assert server.jobs.read()["status"] == "cancelled"
            assert cancel(identifier) == 400
        finally:
            release.set()
            server.jobs.stop(3)
            server.stopping.set()
            thread.join(timeout=3)
