import json
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import Request, urlopen

import pytest

from fba.runtime.local import InstanceLock


def wait_until(predicate, seconds=10):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.02)
    raise AssertionError("local process did not reach the expected state")


@pytest.mark.parametrize("force", [False, True])
def test_simultaneous_start_and_restart_with_chinese_path(tmp_path, force):
    root = tmp_path / "季賽 測試資料"
    script = tmp_path / "launch.py"
    script.write_text(
        "import sys, webbrowser\nfrom pathlib import Path\n"
        "from fba.apps.inseason.server import serve_inseason\n"
        "root = Path(sys.argv[1])\n"
        "def opened(url):\n    (root / 'browser-opened').write_text('opened')\n    return True\n"
        "webbrowser.open = opened\n"
        "serve_inseason(root, open_browser=False)\n"
    )
    processes = []

    def start():
        process = subprocess.Popen(
            [sys.executable, str(script), str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        processes.append(process)
        return process

    try:
        first, second = start(), start()
        wait_until(lambda: first.poll() is not None or second.poll() is not None)
        running, duplicate = (first, second) if first.poll() is None else (second, first)
        _, error = duplicate.communicate(timeout=2)
        assert duplicate.returncode == 0, error.decode()
        assert running.poll() is None
        assert (root / "browser-opened").read_text() == "opened"
        instance = InstanceLock(root / "inseason.lock").read()
        assert instance.pid == running.pid
        origin = f"http://127.0.0.1:{instance.port}"
        with urlopen(
            Request(origin + "/health", headers={"Authorization": "Bearer " + instance.token})
        ) as response:
            assert json.load(response)["pid"] == running.pid
        if force:
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
                )
            ) as response:
                assert json.load(response)["stopping"]
        running.communicate(timeout=5)
        restarted = start()
        wait_until(lambda: InstanceLock(root / "inseason.lock").read().pid == restarted.pid)
        new = InstanceLock(root / "inseason.lock").read()
        assert new.port == instance.port
        assert new.token != instance.token
        assert restarted.poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


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
