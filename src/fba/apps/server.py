import fcntl
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from pathlib import Path
from typing import override

from fba.adapters.auction import load_auction
from fba.adapters.codec import canonical, decode
from fba.apps.auction import AuctionSession
from fba.apps.desk import AuctionDesk, session_calculator
from fba.contracts.auction import AuctionInput, SolverError
from fba.contracts.base import ConfigError, DataError, Record
from fba.contracts.desk import CompareRequest, DeskError, SaveDraft, StateRequest


class DeskServer(ThreadingHTTPServer):
    def __init__(self, desk: AuctionDesk, port: int) -> None:
        self.desk = desk
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), DeskHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


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
        origin = self.headers.get("Origin")
        valid = self.headers.get("Host") == self.desk_server.origin.removeprefix("http://")
        valid = valid and (origin is None or origin == self.desk_server.origin)
        if api:
            valid = valid and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(),
                f"Bearer {self.desk_server.token}".encode(),
            )
        if not valid:
            self.respond(
                DeskError(error="local service: origin, host or session token rejected"), 403
            )
        return valid

    def do_GET(self) -> None:
        if not self.permitted(api=self.path.startswith("/api/")):
            return
        if self.path == "/api/bootstrap":
            self.respond(self.desk_server.desk.bootstrap())
        elif self.path == "/api/results":
            self.respond(self.desk_server.desk.results())
        else:
            assets = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/view.js": ("view.js", "text/javascript"),
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
                saved = desk.save(decode(SaveDraft, data, "request.draft"))
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
    try:
        with draft.with_suffix(draft.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_server(inputs, sha, draft, log, workers, port)
    except OSError as exc:
        raise DataError(f"serve: cannot open or exclusively lock draft: {exc}") from exc


def run_server(
    inputs: AuctionInput, sha: str, draft: Path, log: Path, workers: int, port: int
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
        )
        with DeskServer(desk, port) as server:
            print(
                json.dumps(
                    {
                        "url": f"{server.origin}/#{server.token}",
                        "draft": str(draft),
                        "log": str(log),
                        "worker_limit": workers,
                    }
                ),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if desk is not None:
            desk.close()
        session.close()
