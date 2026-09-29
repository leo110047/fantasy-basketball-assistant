import fcntl
import json
import os
import threading
from http.client import HTTPConnection
from time import monotonic, sleep

import pytest
from test_auction import config, inputs_for, player, state

import fba.adapters.desk as ledger
from fba.adapters.codec import canonical, decode, digest
from fba.apps.desk import AuctionDesk, LatestCalculation
from fba.apps.server import DeskServer, serve
from fba.contracts.auction import DraftState, Sale
from fba.contracts.base import ConfigError, DataError
from fba.contracts.desk import CompareRequest, DeskError, DeskExecution, SaveDraft
from fba.core.auction import calculate_auction


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

    def calculate(draft, sha):
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
    for line in desk.log.read_bytes().splitlines():
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


def test_open_editor_can_finish_writing_displaced_version_after_save(desk):
    external = canonical(desk.bootstrap().desk.state.model_copy(update={"revision": 99}))
    with desk.path.open("r+b") as editor:
        saved = desk.save(sell_request(desk))
        editor.write(external)
        editor.truncate()
    assert desk.path.read_bytes() == canonical(saved.state)
    backups = list((desk.path.parent / f".{desk.path.name}.history").glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == external


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
    failures = [
        entry
        for line in desk.log.read_bytes().splitlines()
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

    def calculate(state, sha):
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
            headers={"Authorization": f"Bearer {server.token}", **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_actual_http_round_trip_and_static_routes(server):
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

    def run(inputs, sha, draft, log, workers, port):
        calls.append(draft)
        assert draft == desk.path.resolve()
        # A second server through a symlink must fail before it can read or write the ledger.
        with pytest.raises(DataError, match="exclusively lock"):
            serve(desk.path.parent / "input.json", desk.path, desk.log, 1, 0)
        original = ledger.atomic_exchange

        def interleave(source, target):
            with draft.with_suffix(draft.suffix + ".lock").open("a") as editor:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(editor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original(source, target)

        monkeypatch.setattr(ledger, "atomic_exchange", interleave)
        desk.save(sell_request(desk))

    monkeypatch.setattr("fba.apps.server.run_server", run)
    serve(desk.path.parent / "input.json", alias, desk.log, 1, 0)
    assert calls == [desk.path.resolve()]
    # Once the service exits, an editor honoring the protocol may save normally.
    with desk.path.with_suffix(".json.lock").open("a") as editor:
        fcntl.flock(editor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        edited = desk.bootstrap().desk.state.model_copy(update={"revision": 99})
        desk.path.write_bytes(canonical(edited))
    assert decode(DraftState, desk.path.read_bytes(), "edited") == edited
