import json
import os
import threading
from concurrent.futures import CancelledError
from http.client import HTTPConnection
from time import monotonic, sleep

import portalocker
import pytest
from test_auction import config, inputs_for, player, state

import fba.adapters.desk as ledger
from fba.apps.desk import AuctionDesk, LatestCalculation
from fba.apps.server import DeskServer, serve
from fba.auction.auction import calculate_auction
from fba.contracts.auction import DraftState, Sale
from fba.contracts.base import ConfigError, DataError
from fba.contracts.desk import CompareRequest, DeskError, DeskExecution, SaveDraft
from fba.data.codec import canonical, decode, digest


def wait_for(predicate):
    deadline = monotonic() + 10
    while not predicate():
        assert monotonic() < deadline, "background calculation timed out"
        sleep(0.01)


@pytest.fixture
def desk(tmp_path):
    league = config().league
    league = league.model_copy(
        update={"teams": 2, "starter_slots": league.starter_slots[:2], "bench_slots": 0}
    )
    inputs = inputs_for(tuple(player(i, ("PG", "SG", "SF", "PF", "C")) for i in range(25)), league)
    initial = state(inputs)
    path = tmp_path / "draft.json"
    path.write_bytes(canonical(initial))

    def calculate(draft, sha, cancelled):
        return calculate_auction(inputs, draft, "0" * 64, sha)

    service = AuctionDesk(
        inputs, "0" * 64, path, tmp_path / "execution.jsonl", calculate, calculate
    )
    try:
        yield service
    finally:
        service.close()


def sell_request(desk, player_id="000", amount=1):
    draft = desk.bootstrap().desk.state
    sale = Sale(id=str(len(draft.sales)), player_id=player_id, buyer=draft.mine, amount=amount)
    return SaveDraft(
        expected_sha256=desk.results().state_sha256,
        draft=draft.model_copy(update={"sales": (*draft.sales, sale)}),
    )


def test_save_undo_backup_restore_and_disk_reload(desk):
    original = desk.bootstrap().desk
    saved = desk.save(sell_request(desk))
    assert saved.state.revision == 1
    assert decode(DraftState, desk.path.read_bytes(), "test") == saved.state
    assert saved.market.market.room[0].budget == original.market.market.room[0].budget - 1
    backup = canonical(saved.state)
    undone = desk.save(SaveDraft(expected_sha256=saved.market.state_sha256, draft=original.state))
    assert not undone.state.sales
    restored = desk.save(
        SaveDraft(
            expected_sha256=undone.market.state_sha256, draft=decode(DraftState, backup, "backup")
        )
    )
    assert restored.state.sales == saved.state.sales
    assert restored.state.revision == 3
    desk.start_calculations(restored.market.state_sha256)
    wait_for(lambda: desk.results().equal.status == "ready")
    with desk.log_lock:
        lines = desk.log.read_bytes().splitlines()
    for line in lines:
        entry = decode(DeskExecution, line, "log")
        assert entry.state_sha256 == digest(canonical(entry.state))
        assert entry.result.state_sha256 == entry.state_sha256


def test_invalid_conflicting_stale_and_external_edits_preserve_ledger(desk):
    before = desk.path.read_bytes()
    request = sell_request(desk, amount=999)
    with pytest.raises(DataError, match="legal bid"):
        desk.save(request)
    assert desk.path.read_bytes() == before
    request = sell_request(desk)
    wrong = request.draft.model_copy(update={"input_sha256": "a" * 64})
    with pytest.raises(DataError, match="hash mismatch"):
        desk.save(request.model_copy(update={"draft": wrong}))
    desk.save(request)
    with pytest.raises(DataError, match="another change"):
        desk.save(request)
    current = desk.bootstrap().desk
    desk.path.write_bytes(b"external edit")
    with pytest.raises(DataError, match="outside this session"):
        desk.save(sell_request(desk, "001"))
    assert desk.bootstrap().desk == current
    assert desk.path.read_bytes() == b"external edit"


def test_failed_disk_replace_is_not_a_saved_sale(desk, monkeypatch):
    before = desk.bootstrap().desk

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "atomic_exchange", fail)
    with pytest.raises(DataError, match="disk full"):
        desk.save(sell_request(desk))
    assert desk.bootstrap().desk == before
    assert desk.path.read_bytes() == canonical(before.state)
    assert not list((desk.path.parent / f".{desk.path.name}.history").iterdir())


@pytest.mark.parametrize("external_save", ["in-place", "replace"])
def test_uncooperative_edit_between_check_and_save_remains_recoverable(
    desk, monkeypatch, external_save
):
    external = desk.bootstrap().desk.state.model_copy(update={"revision": 99})
    external_bytes = canonical(external)
    original = desk.path.read_bytes()

    def interleaved_read(path):
        assert path.read_bytes() == original
        if external_save == "in-place":
            path.write_bytes(external_bytes)
        else:
            editor = path.with_suffix(".editor")
            editor.write_bytes(external_bytes)
            os.replace(editor, path)
        return original

    monkeypatch.setattr("fba.adapters.desk.read_bytes", interleaved_read)
    saved = desk.save(sell_request(desk))
    assert desk.path.read_bytes() == canonical(saved.state)
    backups = list((desk.path.parent / f".{desk.path.name}.history").glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == external_bytes


def test_atomic_exchange_failure_does_not_remove_either_file(tmp_path):
    source, target = tmp_path / "source", tmp_path / "missing"
    source.write_bytes(b"preserve candidate")
    with pytest.raises(OSError):
        ledger.atomic_exchange(source, target)
    assert source.read_bytes() == b"preserve candidate"
    assert not target.exists()


def test_background_failure_keeps_saved_sales_and_retry_recovers(desk):
    wait_for(lambda: desk.results().equal.status == "ready")
    good = desk.equal.calculate
    entered = threading.Event()

    def fail(*args):
        entered.set()
        raise RuntimeError("injected solver failure")

    desk.equal.calculate = fail
    saved = desk.save(sell_request(desk))
    desk.start_calculations(saved.market.state_sha256)
    assert entered.wait(5)
    wait_for(lambda: desk.results().equal.status == "failed")
    assert desk.results().equal.result is None
    assert desk.path.read_bytes() == canonical(saved.state)
    with desk.log_lock:
        lines = desk.log.read_bytes().splitlines()
    failures = [
        entry
        for line in lines
        if isinstance((entry := decode(DeskExecution, line, "log")).result, DeskError)
    ]
    assert len(failures) == 1
    assert failures[0].state == saved.state
    assert failures[0].state_sha256 == saved.market.state_sha256
    assert failures[0].elapsed_ns > 0
    assert failures[0].solver_calls is None
    assert failures[0].result.error == "RuntimeError: injected solver failure"
    desk.equal.calculate = good
    desk.retry(saved.market.state_sha256)
    wait_for(lambda: desk.results().equal.status == "ready")
    assert desk.results().equal.result.state_sha256 == saved.market.state_sha256


def test_log_failure_is_visible_and_worker_can_retry(desk):
    wait_for(lambda: desk.results().equal.status == "ready")
    record = desk.equal.record

    def unavailable(entry):
        raise OSError("disk full")

    desk.equal.record = unavailable
    sha = desk.results().state_sha256
    desk.retry(sha)
    wait_for(lambda: desk.results().equal.status == "failed")
    assert "failure log unavailable: OSError: disk full" in desk.results().equal.error
    assert desk.equal.thread.is_alive()
    desk.equal.record = record
    desk.retry(sha)
    wait_for(lambda: desk.results().equal.status == "ready")


def test_running_result_cannot_replace_newer_state_and_pending_work_coalesces(desk):
    entered, release = threading.Event(), threading.Event()
    calls, records = [], []
    draft = desk.bootstrap().desk.state

    def calculate(state, sha, cancelled):
        calls.append(state.revision)
        if state.revision == 0:
            entered.set()
            assert release.wait(5)
        return calculate_auction(desk.inputs, state, desk.input_hash, sha)

    job = LatestCalculation(calculate, records.append, "equal")
    try:
        job.request(draft, digest(canonical(draft)))
        assert entered.wait(5)
        for revision in (1, 2, 3):
            candidate = draft.model_copy(update={"revision": revision})
            sha = digest(canonical(candidate))
            job.request(candidate, sha)
        assert job.current(sha).status == "updating"
        assert job.current(sha).result is None
        release.set()
        wait_for(lambda: job.current(sha).status == "ready")
        assert calls == [0, 3]
        assert job.current(sha).result.state_sha256 == sha
    finally:
        release.set()
        job.close()


def test_compare_uses_exact_state_and_refuses_busy_or_stale(desk):
    wait_for(lambda: desk.results().equal.status == "ready")
    sha = desk.results().state_sha256
    request = CompareRequest(state_sha256=sha, player_id="000", price=1, mode="equal")
    result = desk.comparison(request)
    assert result.state_sha256 == sha
    assert result.comparison.buy.players
    desk.save(sell_request(desk))
    with pytest.raises(DataError, match="another change"):
        desk.comparison(request)


def test_new_request_and_close_signal_running_calculations(desk):
    entered = threading.Event()
    records = []
    draft = desk.bootstrap().desk.state

    def calculate(state, sha, cancelled):
        if state.revision in (0, 2):
            entered.set()
            assert cancelled.wait(5), "obsolete computation was not signalled"
            raise CancelledError("superseded")
        return calculate_auction(desk.inputs, state, desk.input_hash, sha)

    job = LatestCalculation(calculate, records.append, "equal")
    try:
        job.request(draft, digest(canonical(draft)))
        assert entered.wait(5)
        latest = draft.model_copy(update={"revision": 1})
        sha = digest(canonical(latest))
        job.request(latest, sha)
        wait_for(lambda: job.current(sha).status == "ready")
        assert len(records) == 1 and records[0].state.revision == 1
        entered.clear()
        closing = draft.model_copy(update={"revision": 2})
        job.request(closing, digest(canonical(closing)))
        assert entered.wait(5)
    finally:
        job.close()
    assert not job.thread.is_alive()
    assert len(records) == 1


def test_only_successfully_saved_changes_cancel_the_old_work(desk):
    before = (desk.equal.cancelled, desk.fit.cancelled)
    with pytest.raises(DataError, match="legal bid"):
        desk.save(sell_request(desk, amount=999))
    assert not any(event.is_set() for event in before)
    saved = desk.save(sell_request(desk))
    assert all(event.is_set() for event in before)
    assert desk.path.read_bytes() == canonical(saved.state)
    assert desk.results().fit.status == "updating"


def test_team_names_and_watch_lists_keep_ready_numerical_results(desk, monkeypatch):
    wait_for(lambda: desk.results().equal.status == desk.results().fit.status == "ready")
    before = desk.bootstrap().desk
    previous = desk.results()

    def forbidden(*args):
        pytest.fail("metadata-only save restarted a calculation")

    monkeypatch.setattr(desk.equal, "calculate", forbidden)
    monkeypatch.setattr(desk.fit, "calculate", forbidden)
    monkeypatch.setattr(desk, "market", forbidden)
    teams = tuple(t.model_copy(update={"name": f"New {t.name}"}) for t in before.state.teams)
    candidate = before.state.model_copy(update={"teams": teams, "watch": ("000", "001")})
    saved = desk.save(SaveDraft(expected_sha256=before.market.state_sha256, draft=candidate))
    desk.start_calculations(saved.market.state_sha256)
    current = desk.results()
    assert current.equal.status == current.fit.status == "ready"
    for old, new in ((previous.equal, current.equal), (previous.fit, current.fit)):
        assert old.result.model_copy(update={"state_sha256": current.state_sha256}) == new.result
    assert decode(DraftState, desk.path.read_bytes(), "backup").watch == ("000", "001")
    # Restoring an older backup preserves its explicitly empty watch list.
    restored = desk.save(SaveDraft(expected_sha256=current.state_sha256, draft=before.state))
    assert restored.state.watch == ()


def test_metadata_change_during_calculation_keeps_work_and_current_labels(desk):
    from threading import Event

    entered, release = Event(), Event()
    records, calls = [], []
    before = desk.bootstrap().desk

    def calculate(draft, sha, cancelled):
        calls.append(sha)
        entered.set()
        assert release.wait(5)
        assert not cancelled.is_set()
        return calculate_auction(desk.inputs, draft, desk.input_hash, sha)

    job = LatestCalculation(calculate, records.append, "equal")
    try:
        job.request(before.state, before.market.state_sha256)
        assert entered.wait(5)
        candidate = before.state.model_copy(update={"revision": 1, "watch": ("001",)})
        sha = digest(canonical(candidate))
        assert job.relabel(candidate, sha)
        release.set()
        wait_for(lambda: job.current(sha).status == "ready")
        assert calls == [before.market.state_sha256]
        assert records[0].state == candidate
        assert records[0].result.state_sha256 == sha
    finally:
        release.set()
        job.close()


def test_metadata_save_before_new_calculation_starts_does_not_reuse_old_work(desk):
    sold = desk.save(sell_request(desk))
    candidate = sold.state.model_copy(update={"watch": ("001",)})
    saved = desk.save(SaveDraft(expected_sha256=sold.market.state_sha256, draft=candidate))
    desk.start_calculations(saved.market.state_sha256)
    wait_for(lambda: desk.results().equal.status == desk.results().fit.status == "ready")
    assert desk.results().equal.result.plan.players.count("000") == 1
    assert desk.results().equal.result.state_sha256 == saved.market.state_sha256


@pytest.mark.parametrize("watch", [("missing",), ("000", "000")])
def test_invalid_watch_lists_cannot_be_imported(desk, watch):
    before = desk.bootstrap().desk
    candidate = before.state.model_copy(update={"watch": watch})
    with pytest.raises(ConfigError, match="draft.watch"):
        desk.save(SaveDraft(expected_sha256=before.market.state_sha256, draft=candidate))
    assert desk.bootstrap().desk == before


def test_service_shares_background_pool_and_comparison_never_queues_behind_it(desk, monkeypatch):
    import fba.apps.server as server
    from fba.auction.auction import comparison_plans

    owners = []

    def calculator(inputs, sha, session, mode):
        owners.append(session)
        return lambda state, state_sha, cancelled: calculate_auction(inputs, state, sha, state_sha)

    class ProbeServer:
        origin, token = "http://127.0.0.1:0", "test"

        def __init__(self, service, port):
            self.service = service
            self.stopping = threading.Event()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def start(self, service, stopping):
            self.service = service
            assert owners[0] is owners[1]
            assert self.service.comparison_runner is comparison_plans
            wait_for(lambda: self.service.results().equal.status == "ready")
            from fba.auction.auction import compare, market_context, portfolio_for

            draft = self.service.bootstrap().desk.state
            players, market = market_context(desk.inputs, draft, desk.input_hash)
            for candidate, price in (("001", 1), ("004", 5), ("007", 100)):
                expected = compare(
                    portfolio_for(desk.inputs, players, market, draft), candidate, price
                )
                result = self.service.comparison(
                    CompareRequest(
                        state_sha256=self.service.results().state_sha256,
                        player_id=candidate,
                        price=price,
                        mode="equal",
                    )
                )
                assert canonical(result.comparison) == canonical(expected)

    monkeypatch.setattr(server, "session_calculator", calculator)
    monkeypatch.setattr(server, "DeskServer", ProbeServer)
    path = desk.path.with_name("service-draft.json")
    path.write_bytes(canonical(sell_request(desk).draft))
    server.run_server(desk.inputs, desk.input_hash, path, path.with_suffix(".jsonl"), 2, 0)


@pytest.fixture
def server(desk):
    with DeskServer(desk, 0) as http:
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            yield http
        finally:
            http.shutdown()
            thread.join()


def request(server, method, path, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {server.token}",
                "Origin": server.origin,
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_actual_http_round_trip_and_static_routes(server):
    assert request(server, "GET", "/api/health")[0] == 200
    status, headers, payload = request(server, "GET", "/api/bootstrap")
    assert status == 200
    initial = json.loads(payload)
    assert initial["league"]["teams"] == len(initial["desk"]["state"]["teams"])
    assert headers["Cache-Control"] == "no-store"
    for path in ("/", "/app.js", "/view.js", "/timing.js", "/style.css"):
        status, _, body = request(server, "GET", path)
        assert status == 200 and body
    status, _, payload = request(
        server,
        "POST",
        "/api/draft",
        canonical(sell_request(server.desk)),
        {"Content-Type": "application/json"},
    )
    assert status == 200 and len(json.loads(payload)["state"]["sales"]) == 1
    status, _, payload = request(server, "GET", "/api/results")
    assert (
        status == 200 and json.loads(payload)["state_sha256"] == server.desk.results().state_sha256
    )
    assert request(server, "GET", "/../draft.json")[0] == 404


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Bearer bad"},
        {"Authorization": "\u00ff"},
        {"Origin": "https://example.invalid"},
        {"Host": "evil.invalid"},
    ],
)
def test_loopback_host_origin_and_token_boundary(server, headers):
    before = server.desk.path.read_bytes()
    assert request(server, "GET", "/api/bootstrap", headers=headers)[0] == 403
    assert request(server, "GET", "/api/health", headers=headers)[0] == 403
    assert request(server, "POST", "/api/draft", b"{}", headers)[0] == 403
    assert server.desk.path.read_bytes() == before


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"{}", {}),
        (b"{", {"Content-Type": "application/json"}),
        (b"{}", {"Content-Type": "application/json", "Content-Length": "2000001"}),
        (b"{}", {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}),
    ],
)
def test_invalid_http_payload_does_not_change_ledger(server, body, headers):
    before = server.desk.path.read_bytes()
    assert request(server, "POST", "/api/draft", body, headers)[0] == 400
    assert server.desk.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("alias_pair", [(0, 1), (0, 2), (1, 2)])
def test_service_rejects_aliasing_input_draft_and_log_before_writing(tmp_path, kind, alias_pair):
    paths = [tmp_path / name for name in ("input.json", "draft.json", "log.jsonl")]
    path, link = (paths[i] for i in alias_pair)
    path.write_bytes(b"preserve input")
    if kind == "symlink":
        link.symlink_to(path)
    else:
        os.link(path, link)
    with pytest.raises(ConfigError, match="distinct files"):
        serve(*paths, 1, 0)
    assert path.read_bytes() == b"preserve input"


def test_all_service_writers_share_the_canonical_draft_lock(desk, monkeypatch):
    alias = desk.path.parent / "draft-alias.json"
    alias.symlink_to(desk.path)
    monkeypatch.setattr("fba.apps.server.load_auction", lambda path: (desk.inputs, desk.input_hash))
    calls = []

    def run(inputs, sha, draft, log, workers, port, instance_lock, *, open_browser, launch_key):
        assert open_browser is True
        assert launch_key
        calls.append(draft)
        assert draft == desk.path.resolve()
        # A second server through a symlink must fail before it can read or write the ledger.
        with pytest.raises(DataError, match="已在執行但沒有回應"):
            serve(desk.path.parent / "input.json", desk.path, desk.log, 1, 0)
        original = ledger.atomic_exchange

        def interleave(source, target):
            with draft.with_suffix(draft.suffix + ".lock").open("a") as editor:
                with pytest.raises(portalocker.exceptions.LockException):
                    portalocker.lock(editor, portalocker.LOCK_EX | portalocker.LOCK_NB)
            original(source, target)

        monkeypatch.setattr(ledger, "atomic_exchange", interleave)
        desk.save(sell_request(desk))

    monkeypatch.setattr("fba.apps.server.run_server", run)
    serve(desk.path.parent / "input.json", alias, desk.log, 1, 0)
    assert calls == [desk.path.resolve()]
    # Once the service exits, an editor honoring the protocol may save normally.
    with desk.path.with_suffix(".json.lock").open("a") as editor:
        portalocker.lock(editor, portalocker.LOCK_EX | portalocker.LOCK_NB)
        edited = desk.bootstrap().desk.state.model_copy(update={"revision": 99})
        desk.path.write_bytes(canonical(edited))
    assert decode(DraftState, desk.path.read_bytes(), "edited") == edited
