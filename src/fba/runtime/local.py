import errno
import hmac
import json
import os
import secrets
import signal
import socket
import sys
import webbrowser
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import FrameType
from typing import IO
from urllib.error import URLError
from urllib.request import Request, urlopen

from platformdirs import user_data_path
from pydantic import AwareDatetime

from fba.contracts.base import DataError, Record, Text
from fba.data.codec import canonical, decode


class Instance(Record):
    app: Text
    pid: int
    port: int
    token: Text
    started_at: AwareDatetime


def data_directory() -> Path:
    return user_data_path("Fantasy Basketball Assistant", appauthor=False, roaming=True)


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: IO[str] | None = None

    def acquire(self) -> bool:
        import portalocker

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, OSError) as exc:
            stream.close()
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            ):
                raise
            return False
        os.chmod(self.path, 0o600)
        self.stream = stream
        return True

    def publish(self, instance: Instance) -> None:
        if self.stream is None:
            raise DataError("runtime.lock: not held")
        # Windows byte locks deny reads as well. Lock byte zero only and keep
        # instance metadata after it so another process can health-check it.
        self.stream.seek(1 if sys.platform == "win32" else 0)
        self.stream.truncate()
        self.stream.write(canonical(instance).decode())
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def read(self) -> Instance:
        with self.path.open("rb") as stream:
            stream.seek(1 if sys.platform == "win32" else 0)
            return decode(Instance, stream.read(), str(self.path))

    def release(self) -> None:
        import portalocker

        if self.stream is not None:
            if sys.platform == "win32":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                portalocker.unlock(self.stream)
            self.stream.close()
            self.stream = None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, handler: type[BaseHTTPRequestHandler], attempts: int = 1) -> None:
        self.token = secrets.token_urlsafe(32)
        self.started_at = datetime.now(UTC)
        self.stopping = Event()
        super().__init__(("127.0.0.1", port), handler, bind_and_activate=False)
        try:
            if sys.platform == "win32":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ports = (
                [0] if port == 0 else [p for p in range(port, min(65536, port + attempts))] + [0]
            )
            for candidate in ports:
                try:
                    self.server_address = ("127.0.0.1", candidate)
                    self.server_bind()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EADDRINUSE, errno.EACCES) or candidate == 0:
                        raise
            self.server_activate()
        except OSError as exc:
            self.server_close()
            raise DataError(f"runtime: cannot bind loopback socket ({exc.errno})") from exc
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.timeout = 0.1

    def run(self) -> None:
        while not self.stopping.is_set():
            self.handle_request()


def permitted(
    headers: dict[str, str], port: int, token: str, *, authenticated: bool, mutation: bool
) -> bool:
    if headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    origin = headers.get("Origin")
    origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    if (mutation and origin not in origins) or (origin is not None and origin not in origins):
        return False
    return not authenticated or hmac.compare_digest(
        headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
    )


def open_existing(lock: InstanceLock, app: str) -> None:
    deadline = monotonic() + 2
    instance: Instance | None = None
    while monotonic() < deadline:
        try:
            instance = lock.read()
            if instance.app != app or not 0 < instance.port <= 65535:
                raise DataError("runtime.lock: invalid instance metadata")
            url = f"http://127.0.0.1:{instance.port}"
            with urlopen(
                Request(url + "/health", headers={"Authorization": "Bearer " + instance.token}),
                timeout=max(0.05, deadline - monotonic()),
            ) as response:
                result = decode(Instance, response.read(4096), "runtime.health")
            if result == instance:
                webbrowser.open(url + "/#" + instance.token)
                return
        except (URLError, OSError, DataError):
            # Another process can hold the lock before it has published metadata
            # or started accepting requests. Retry only within this startup grace.
            sleep(0.05)
    pid = str(instance.pid) if instance else "尚未公布"
    raise DataError(f"助手已在執行但沒有回應；PID {pid}。請檢查該行程後再啟動。")


@contextmanager
def shutdown_signals(stopping: Event | None = None) -> Generator[Event]:
    event = stopping or Event()

    def stop(_signum: int, _frame: FrameType | None) -> None:
        event.set()

    watched = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        watched.append(signal.SIGBREAK)
    if hasattr(signal, "SIGHUP"):
        watched.append(signal.SIGHUP)
    previous = {s: signal.signal(s, stop) for s in watched}
    try:
        yield event
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)


def launch_browser(server: LocalServer, *, open_browser: bool) -> None:
    # The fragment is passed to the browser only, never to HTTP logs or stdout.
    if sys.stdout is not None:
        print(json.dumps({"url": server.origin, "pid": os.getpid()}), flush=True)
    if open_browser:
        webbrowser.open(server.origin + "/#" + server.token)
