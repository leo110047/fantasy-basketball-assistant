import json
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from fba.contracts.base import DataError
from fba.runtime.local import InstanceLock


def wait_until(predicate, seconds=10):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.02)
    raise AssertionError("local process did not reach the expected state")


def published_pid(path):
    try:
        return InstanceLock(path).read().pid
    except (DataError, FileNotFoundError):
        # Publishing truncates then writes under the lifetime lock. Production
        # open_existing already retries within a bounded startup grace period.
        return None


def assert_http_stopped(origin, token, deadline):
    # Use the same five-second stop deadline, including process and TCP cleanup.
    # A Windows closed listener can take about two seconds to refuse a connection.
    remaining = deadline - monotonic()
    assert remaining > 0, "server exceeded the stop deadline"
    with (
        pytest.raises(URLError) as stopped,
        urlopen(
            Request(origin + "/health", headers={"Authorization": "Bearer " + token}),
            timeout=remaining,
        ),
    ):
        pass
    assert isinstance(stopped.value.reason, ConnectionRefusedError)
    assert monotonic() <= deadline, "server exceeded the stop deadline"


@pytest.mark.parametrize("status", [200, 401])
def test_stop_probe_rejects_a_live_http_service(status):
    class Healthy(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with HTTPServer(("127.0.0.1", 0), Healthy) as server:
        server.timeout = 1
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            failure = pytest.fail.Exception if status == 200 else AssertionError
            with pytest.raises(failure):
                assert_http_stopped(
                    f"http://127.0.0.1:{server.server_port}", "fixture", monotonic() + 1
                )
        finally:
            thread.join(timeout=1)


def test_stop_probe_rejects_an_unresponsive_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        # The connection is accepted by TCP, but no HTTP response is produced.
        with pytest.raises((TimeoutError, AssertionError)):
            assert_http_stopped(
                f"http://127.0.0.1:{listener.getsockname()[1]}", "fixture", monotonic() + 0.05
            )


@pytest.mark.parametrize("force", [False, True])
def test_simultaneous_start_and_restart_with_chinese_path(tmp_path, force):
    root = tmp_path / "季賽 測試資料"
    script = tmp_path / "launch.py"
    script.write_text(
        "import os, sys, webbrowser\nfrom pathlib import Path\n"
        "Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "from fba.apps.inseason.server import serve_inseason\n"
        "root = Path(sys.argv[1])\n"
        "def opened(url):\n    (root / 'browser-opened').write_text('opened')\n    return True\n"
        "webbrowser.open = opened\n"
        "serve_inseason(root, open_browser=False)\n"
    )
    processes = []
    pid_files = {}

    def start():
        pid_file = tmp_path / f"interpreter-{len(processes)}.pid"
        process = subprocess.Popen(
            [sys.executable, str(script), str(root), str(pid_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        pid_files[process.pid] = pid_file
        return process

    def interpreter_pid(process):
        # Windows venv python.exe is a launcher, not the actual interpreter.
        # The child reports its own PID independently of the lock and health API.
        try:
            return int(pid_files[process.pid].read_text())
        except (FileNotFoundError, ValueError):
            return None

    try:
        first, second = start(), start()
        wait_until(lambda: first.poll() is not None or second.poll() is not None)
        running, duplicate = (first, second) if first.poll() is None else (second, first)
        _, error = duplicate.communicate(timeout=2)
        assert duplicate.returncode == 0, error.decode()
        assert running.poll() is None
        assert (root / "browser-opened").read_text() == "opened"
        instance = InstanceLock(root / "inseason.lock").read()
        assert instance.pid == interpreter_pid(running)
        origin = f"http://127.0.0.1:{instance.port}"
        with urlopen(
            Request(origin + "/health", headers={"Authorization": "Bearer " + instance.token})
        ) as response:
            assert json.load(response)["pid"] == interpreter_pid(running)
        stop_deadline = monotonic() + 5
        if force:
            # Deliberately terminate the launcher: its native job must also
            # terminate the interpreter, release the lock and close its pipes.
            # CPython v3.13.7 PC/venvlauncher.c uses KILL_ON_JOB_CLOSE;
            # uv 0.9.18 uv-virtualenv/src/virtualenv.rs copies that launcher.
            running.kill()
        else:
            with urlopen(
                Request(
                    origin + "/api/quit",
                    data=b"{}",
                    headers={
                        "Authorization": "Bearer " + instance.token,
                        "Origin": origin,
                        "Content-Type": "application/json",
                    },
                ),
                timeout=stop_deadline - monotonic(),
            ) as response:
                assert json.load(response)["stopping"]
        running.communicate(timeout=stop_deadline - monotonic())
        assert_http_stopped(origin, instance.token, stop_deadline)
        restarted = start()
        wait_until(
            lambda: (
                (pid := interpreter_pid(restarted)) is not None
                and published_pid(root / "inseason.lock") == pid
            )
        )
        new = InstanceLock(root / "inseason.lock").read()
        assert new.port == instance.port
        assert new.token != instance.token
        assert restarted.poll() is None
        with urlopen(
            Request(origin + "/health", headers={"Authorization": "Bearer " + new.token}),
            timeout=2,
        ) as response:
            assert json.load(response)["pid"] == interpreter_pid(restarted)
    finally:
        cleanup_deadline = monotonic() + 5
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=cleanup_deadline - monotonic())
    assert_http_stopped(origin, new.token, cleanup_deadline)


@pytest.mark.parametrize("error", [10013, 10048])
def test_windows_exclusive_bind_retries_reserved_or_busy_ports(monkeypatch, error):
    from http.server import BaseHTTPRequestHandler
    from types import SimpleNamespace

    from fba.runtime import local

    options, bound = [], []

    class Socket:
        def setsockopt(self, level, name, value):
            options.append((level, name, value))

        def bind(self, address):
            bound.append(address)
            if len(bound) == 1:
                raise OSError(error, "Windows port unavailable")

        def getsockname(self):
            return bound[-1]

        def listen(self, backlog):
            pass

        def close(self):
            pass

    exclusive = -5
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(local.socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
    monkeypatch.setattr(local.socket, "socket", lambda *args: Socket())
    monkeypatch.setattr(local.socket, "getfqdn", lambda host: host)
    server = local.LocalServer(8766, BaseHTTPRequestHandler, attempts=2)
    try:
        assert bound == [("127.0.0.1", 8766), ("127.0.0.1", 8767)]
        assert options == [(local.socket.SOL_SOCKET, exclusive, 1)]
        assert server.origin == "http://127.0.0.1:8767"
    finally:
        server.server_close()
