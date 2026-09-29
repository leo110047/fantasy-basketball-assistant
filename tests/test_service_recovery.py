import json
import os
import selectors
import signal
import subprocess
import sys
from time import monotonic, sleep

import pytest
from test_desk import desk as desk
from test_desk import sell_request, wait_for

import fba.adapters.desk as ledger
from fba.adapters.codec import canonical
from fba.apps.server import DeskServer, run_server
from fba.contracts.base import DataError
from fba.contracts.desk import SaveDraft, SaveUnconfirmed


def test_failed_save_does_not_log_an_uncommitted_market(desk, monkeypatch):
    before = desk.bootstrap().desk

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "atomic_exchange", fail)
    with pytest.raises(DataError, match="disk full"):
        desk.save(sell_request(desk))
    entries = [json.loads(line) for line in desk.log.read_bytes().splitlines()]
    assert all(e["state_sha256"] == before.market.state_sha256 for e in entries)


@pytest.mark.parametrize("failure", ["history", "directory", "log"])
def test_after_exchange_failure_preserves_current_state_and_reports_uncertainty(
    desk, monkeypatch, failure
):
    original = desk.path.read_bytes()

    def fail(*args):
        raise OSError("durability failure")

    if failure == "log":

        def fail_log(*args):
            raise DataError("log failure")

        monkeypatch.setattr(desk, "record", fail_log)
    else:
        monkeypatch.setattr(
            ledger.os if failure == "history" else ledger,
            "link" if failure == "history" else "sync_directory",
            fail,
        )
    with pytest.raises(SaveUnconfirmed):
        desk.save(sell_request(desk))
    saved = desk.bootstrap().desk
    assert saved.state.revision == 1
    assert desk.path.read_bytes() == desk.payload == canonical(saved.state)
    history = desk.path.parent / f".{desk.path.name}.history"
    candidates = list(history.iterdir())
    assert len(candidates) == 1 and candidates[0].read_bytes() == original
    assert candidates[0].suffix == (".pending" if failure == "history" else ".json")


def test_history_is_unpublished_until_exchange_and_both_directories_are_synced(desk, monkeypatch):
    exchange, sync = ledger.atomic_exchange, ledger.sync_directory
    history = desk.path.parent / f".{desk.path.name}.history"
    synced = []

    def inspect(source, target):
        assert source.suffix == ".pending"
        assert not list(history.glob("*.json"))
        exchange(source, target)

    def record(path):
        synced.append(path)
        sync(path)

    monkeypatch.setattr(ledger, "atomic_exchange", inspect)
    monkeypatch.setattr(ledger, "sync_directory", record)
    desk.save(sell_request(desk))
    assert synced == [history, desk.path.parent]
    assert not list(history.glob("*.pending"))


@pytest.mark.parametrize("change", ["identity", "blank", "duplicate"])
def test_import_preserves_identity_and_validates_names(desk, change):
    before = desk.bootstrap().desk
    candidate = before.state
    if change == "identity":
        candidate = candidate.model_copy(update={"draft_id": "different"})
    else:
        teams = list(candidate.teams)
        teams[0] = teams[0].model_copy(
            update={"name": "   " if change == "blank" else teams[1].name.upper()}
        )
        candidate = candidate.model_copy(update={"teams": tuple(teams)})
    with pytest.raises(DataError):
        desk.save(SaveDraft(expected_sha256=before.market.state_sha256, draft=candidate))
    assert desk.bootstrap().desk == before
    assert desk.path.read_bytes() == canonical(before.state)


def test_bind_and_storage_fail_before_background_work_or_logging(desk, monkeypatch):
    def forbidden(*args):
        pytest.fail("background session started before preflight succeeded")

    monkeypatch.setattr("fba.apps.server.AuctionSession", forbidden)
    wait_for(lambda: desk.results().equal.status == desk.results().fit.status == "ready")
    original = desk.log.read_bytes()
    with DeskServer(None, 0) as occupied:
        with pytest.raises(DataError, match="cannot bind"):
            run_server(desk.inputs, desk.input_hash, desk.path, desk.log, 1, occupied.server_port)

    def unsupported(*args):
        raise OSError("unsupported exchange")

    monkeypatch.setattr(ledger, "atomic_exchange", unsupported)
    with pytest.raises(DataError, match="filesystem cannot atomically save"):
        run_server(desk.inputs, desk.input_hash, desk.path, desk.log, 1, 0)
    assert desk.log.read_bytes() == original


def test_nonserver_cli_import_does_not_require_fcntl():
    code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('unavailable platform module')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from fba.apps.cli import parser
command = parser().parse_args(['calculate', 'input.json', '--output', 'results'])
assert command.command == 'calculate'
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def group_exists(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_signal_during_real_native_compilation_cleans_all_owned_processes(
    desk, tmp_path, termination
):
    inputs = tmp_path / "signal-input.json"
    inputs.write_bytes(canonical(desk.inputs))
    draft = tmp_path / "signal-draft.json"
    draft.write_bytes(desk.path.read_bytes())
    script = tmp_path / "signal_service.py"
    script.write_text("""
import json, subprocess, sys
from pathlib import Path
from fba.adapters.codec import decode
from fba.contracts.auction import AuctionInput
from fba.apps.server import run_server
if __name__ == '__main__':
    original = subprocess.Popen
    def observed(*args, **kwargs):
        process = original(*args, **kwargs)
        print(json.dumps({'native_child': process.pid}), flush=True)
        return process
    subprocess.Popen = observed
    root = Path(sys.argv[1])
    inputs = decode(AuctionInput, (root/'signal-input.json').read_bytes(), 'input')
    run_server(inputs, '0'*64, root/'signal-draft.json', root/'signal.jsonl', 2, 0)
""")
    process = subprocess.Popen(
        [sys.executable, "-u", str(script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = monotonic() + 15
            while True:
                assert selector.select(max(0, deadline - monotonic())), "native child not started"
                if b'"native_child"' in process.stdout.readline():
                    break
        os.kill(process.pid, termination)
        _, error = process.communicate(timeout=15)
        assert process.returncode == 0, error.decode()
        deadline = monotonic() + 3
        while group_exists(process.pid) and monotonic() < deadline:
            sleep(0.02)
        assert not group_exists(process.pid), "service left an owned child behind"
    finally:
        if group_exists(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
