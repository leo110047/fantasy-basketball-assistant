import os
from http.server import BaseHTTPRequestHandler
from itertools import combinations
from pathlib import Path
from threading import Event
from typing import override

from fba.adapters.auction import load_auction
from fba.adapters.desk import check_storage
from fba.apps.auction import AuctionSession
from fba.apps.desk import AuctionDesk, session_calculator, session_streaming
from fba.contracts.auction import AuctionInput, SolverError
from fba.contracts.base import ConfigError, DataError, Record
from fba.contracts.desk import (
    CompareRequest,
    DeskError,
    DeskHealth,
    SaveDraft,
    SaveUnconfirmed,
    SensitivityRequest,
    StateRequest,
    StreamingRequest,
)
from fba.data.codec import canonical, decode
from fba.runtime.assets import formula_script
from fba.runtime.local import (
    Instance,
    InstanceLock,
    LocalServer,
    launch_browser,
    open_existing,
    permitted,
    shutdown_signals,
)
from fba.runtime.processes import owned_processes


class DeskServer(LocalServer):
    def __init__(self, desk: AuctionDesk | None, port: int) -> None:
        self._desk = desk
        super().__init__(port, DeskHandler)
        self.instance = Instance(
            app="desk",
            pid=os.getpid(),
            port=self.server_port,
            token=self.token,
            started_at=self.started_at,
        )

    @property
    def desk(self) -> AuctionDesk:
        if self._desk is None:
            raise RuntimeError("serve: auction desk is not initialized")
        return self._desk

    def start(self, desk: AuctionDesk, stopping: Event) -> None:
        self._desk = desk
        self.timeout = 0.1
        while not stopping.is_set():
            self.handle_request()


class DeskHandler(BaseHTTPRequestHandler):
    @property
    def desk_server(self) -> DeskServer:
        assert isinstance(self.server, DeskServer)
        return self.server

    @override
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def send_body(self, status: int, payload: bytes, media: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def respond(self, record: Record, status: int = 200) -> None:
        self.send_body(status, canonical(record), "application/json; charset=utf-8")

    def permitted(self, *, api: bool) -> bool:
        headers = {
            k: self.headers[k] for k in ("Host", "Origin", "Authorization") if k in self.headers
        }
        valid = permitted(
            headers,
            self.desk_server.server_port,
            self.desk_server.token,
            authenticated=api,
            mutation=self.command == "POST",
        )
        if not valid:
            self.respond(
                DeskError(error="local service: origin, host or session token rejected"), 403
            )
        return valid

    def do_GET(self) -> None:
        if not self.permitted(api=self.path.startswith("/api/") or self.path == "/health"):
            return
        if self.path == "/health":
            self.respond(self.desk_server.instance)
        elif self.path == "/formulas.js":
            self.send_body(200, formula_script(), "text/javascript; charset=utf-8")
        elif self.path == "/api/health":
            self.respond(DeskHealth(status="ok"))
        elif self.path == "/api/bootstrap":
            self.respond(self.desk_server.desk.bootstrap())
        elif self.path == "/api/results":
            self.respond(self.desk_server.desk.results())
        else:
            assets = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/streaming.js": ("streaming.js", "text/javascript"),
                "/view.js": ("view.js", "text/javascript"),
                "/presentation.js": ("presentation.js", "text/javascript"),
                "/editing.js": ("editing.js", "text/javascript"),
                "/timing.js": ("timing.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
            }
            entry = assets.get(self.path)
            if entry is None:
                self.respond(DeskError(error="route: not found"), 404)
                return
            name, media = entry
            self.send_body(
                200,
                (Path(__file__).parent / "static" / name).read_bytes(),
                media + "; charset=utf-8",
            )

    def request_body(self) -> bytes:
        if (
            self.headers.get("Content-Type") != "application/json"
            or self.headers.get("Transfer-Encoding") is not None
        ):
            raise DataError("request: requires application/json and Content-Length")
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= 2_000_000:
            raise DataError("request: invalid or excessive Content-Length")
        data = self.rfile.read(int(length))
        if len(data) != int(length):
            raise DataError("request: incomplete body")
        return data

    def do_POST(self) -> None:
        if not self.permitted(api=True):
            return
        try:
            data = self.request_body()
            desk = self.desk_server.desk
            if self.path == "/api/draft":
                try:
                    saved = desk.save(decode(SaveDraft, data, "request.draft"))
                except SaveUnconfirmed:
                    desk.start_calculations(desk.results().state_sha256)
                    raise
                try:
                    self.respond(saved)
                finally:
                    # Deliver the persisted market before CPU work can contend for the GIL.
                    desk.start_calculations(saved.market.state_sha256)
                return
            elif self.path == "/api/retry":
                result = desk.retry(decode(StateRequest, data, "request.retry").state_sha256)
            elif self.path == "/api/compare":
                result = desk.comparison(decode(CompareRequest, data, "request.compare"))
            elif self.path == "/api/sensitivity":
                result = desk.sensitivity(decode(SensitivityRequest, data, "request.sensitivity"))
            elif self.path == "/api/streaming":
                result = desk.streaming(decode(StreamingRequest, data, "request.streaming"))
            else:
                self.respond(DeskError(error="route: not found"), 404)
                return
            self.respond(result)
        except (ConfigError, DataError, SolverError) as exc:
            self.respond(DeskError(error=str(exc)), 400)
        except Exception as exc:
            self.respond(
                DeskError(error=f"local service failure: {type(exc).__name__}: {exc}"), 500
            )


def serve(input_path: Path, draft: Path, log: Path, workers: int, port: int) -> None:
    if not 0 <= port <= 65535:
        raise ConfigError("serve.port: must be between 0 and 65535")
    paths = (input_path.resolve(), draft.resolve(), log.resolve())
    if len(set(paths)) != len(paths) or any(
        a.exists() and b.exists() and a.samefile(b) for a, b in combinations(paths, 2)
    ):
        raise ConfigError("serve: input, draft and log must be distinct files")
    input_path, draft, log = paths
    inputs, sha = load_auction(input_path)
    # Every service alias shares this stable lock, held across all reads and replacements.
    lock = InstanceLock(draft.with_suffix(draft.suffix + ".lock"))
    if not lock.acquire():
        open_existing(lock, "desk")
        return
    try:
        run_server(inputs, sha, draft, log, workers, port, lock)
    finally:
        lock.release()


def run_server(
    inputs: AuctionInput,
    sha: str,
    draft: Path,
    log: Path,
    workers: int,
    port: int,
    instance_lock: InstanceLock | None = None,
) -> None:
    check_storage(draft)
    with shutdown_signals() as stopping, DeskServer(None, port) as server:
        if instance_lock is not None:
            instance_lock.publish(server.instance)
        launch_browser(server, open_browser=instance_lock is not None)
        with owned_processes():
            run_desk(inputs, sha, draft, log, workers, server, stopping)


def run_desk(
    inputs: AuctionInput,
    sha: str,
    draft: Path,
    log: Path,
    workers: int,
    server: DeskServer,
    stopping: Event,
) -> None:
    session = AuctionSession(workers)
    desk: AuctionDesk | None = None
    try:
        desk = AuctionDesk(
            inputs,
            sha,
            draft,
            log,
            session_calculator(inputs, sha, session, "equal"),
            session_calculator(inputs, sha, session, "fit"),
            streaming_runner=session_streaming(inputs, session),
        )
        server.start(desk, stopping)
    finally:
        if desk is not None:
            desk.close()
        session.close()
