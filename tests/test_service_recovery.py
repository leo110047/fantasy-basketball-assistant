import ctypes
import json
import os
import signal
import subprocess
import sys
from queue import Queue
from threading import Thread
from time import monotonic, sleep

import pytest
from test_desk import desk as desk
from test_desk import sell_request, wait_for

import fba.adapters.desk as ledger
from fba.apps.server import run_server
from fba.contracts.base import DataError
from fba.contracts.desk import SaveDraft, SaveUnconfirmed
from fba.data.codec import canonical


def test_failed_save_does_not_log_an_uncommitted_market(desk, monkeypatch):
    before = desk.bootstrap().desk

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "atomic_exchange", fail)
    with pytest.raises(DataError, match="disk full"):
        desk.save(sell_request(desk))
    # Background calculations may be appending a large record. Take the same
    # writer lock to inspect a complete snapshot without excluding any record.
    with desk.log_lock:
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

    # A busy preferred port now falls back by contract; a real bind failure
    # must still prevent workers and log mutation.
    def bind_failure(_server):
        import errno

        raise OSError(errno.EIO, "injected bind failure")

    with monkeypatch.context() as patch:
        patch.setattr("fba.runtime.local.LocalServer.server_bind", bind_failure)
        with pytest.raises(DataError, match="cannot bind"):
            run_server(desk.inputs, desk.input_hash, desk.path, desk.log, 1, 0)

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


def process_exists(pid):
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = library.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    wait = library.WaitForSingleObject
    wait.argtypes, wait.restype = [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ulong
    close = library.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int
    handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        assert ctypes.get_last_error() == 87  # No such process.
        return False
    try:
        state = wait(handle, 0)
        assert state in (0, 258)
        return state == 258
    finally:
        assert close(handle)


def group_exists(pid):
    if sys.platform == "win32":
        return process_exists(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def native_child(process):
    lines = Queue()

    def read():
        for line in iter(process.stdout.readline, b""):
            lines.put(line)
        lines.put(None)

    Thread(target=read, daemon=True).start()
    deadline = monotonic() + 15
    while True:
        remaining = deadline - monotonic()
        assert remaining > 0, "native child start deadline exceeded"
        line = lines.get(timeout=remaining)
        assert line, "service exited before starting native compilation"
        if b'"native_child"' in line:
            return json.loads(line)["native_child"]


@pytest.mark.parametrize(
    "termination",
    [signal.CTRL_BREAK_EVENT]
    if sys.platform == "win32"
    else [signal.SIGTERM, signal.SIGHUP, signal.SIGINT],
)
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
from fba.data.codec import decode
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
        start_new_session=sys.platform != "win32",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    try:
        child = native_child(process)
        os.kill(process.pid, termination)
        _, error = process.communicate(timeout=15)
        assert process.returncode == 0, error.decode()
        deadline = monotonic() + 3
        while (group_exists(process.pid) or process_exists(child)) and monotonic() < deadline:
            sleep(0.02)
        assert not group_exists(process.pid), "service process group remains"
        assert not process_exists(child), "service left an owned child behind"
    finally:
        if group_exists(process.pid):
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
