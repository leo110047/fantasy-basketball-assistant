import json
import os
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from fba.apps.inseason.api import Jobs, parse_body
from fba.contracts.base import DataError
from fba.data.codec import canonical
from fba.inseason.session import InseasonSession
from fba.runtime.assets import court_image, formula_script, workspace_link_script
from fba.runtime.local import (
    Instance,
    InstanceLock,
    LocalServer,
    launch_browser,
    open_existing,
    permitted,
    shutdown_signals,
)


class SeasonServer(LocalServer):
    def __init__(self, session: InseasonSession) -> None:
        self.session = session
        self.jobs = Jobs(session)
        super().__init__(
            session.preferences.preferred_port, SeasonHandler, session.preferences.port_attempts
        )
        self.instance = Instance(
            app="inseason",
            pid=os.getpid(),
            port=self.server_port,
            token=self.token,
            started_at=self.started_at,
        )

    def run_scheduled(self) -> None:
        interval = self.session.preferences.sync_interval_seconds
        next_sync = self.schedule(interval)
        while not self.stopping.is_set():
            self.handle_request()
            if interval != self.session.preferences.sync_interval_seconds:
                interval = self.session.preferences.sync_interval_seconds
                next_sync = self.schedule(interval)
            if monotonic() >= next_sync and self.jobs.status != "running":
                if self.session.preferences.selected_league is not None:
                    self.jobs.start("sync", b"{}")
                next_sync = self.schedule(interval)

    def schedule(self, interval: float) -> float:
        self.session.next_sync_at = datetime.now(UTC) + timedelta(seconds=interval)
        return monotonic() + interval


class SeasonHandler(BaseHTTPRequestHandler):
    @property
    def local(self) -> SeasonServer:
        assert isinstance(self.server, SeasonServer)
        return self.server

    def log_message(self, format: str, *args: object) -> None:
        # Request paths, headers, bodies and OAuth codes are never logged.
        del format, args

    def reply(self, status: int, payload: bytes, media: str = "application/json") -> None:
        self.send_response(status)
        for key, value in {
            "Content-Type": media + "; charset=utf-8",
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; connect-src 'self'; script-src 'self'; "
                "style-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self, mutation: bool) -> bool:
        headers = {
            k: self.headers[k] for k in ("Host", "Origin", "Authorization") if k in self.headers
        }
        valid = permitted(
            headers,
            self.local.server_port,
            self.local.token,
            authenticated=self.path.startswith("/api/") or self.path == "/health",
            mutation=mutation,
        )
        if not valid:
            self.reply(403, b'{"error":"local request rejected"}')
        return valid

    def do_GET(self) -> None:
        if not self.allowed(False):
            return
        try:
            if self.path == "/health":
                self.reply(200, canonical(self.local.instance))
            elif self.path == "/formulas.js":
                self.reply(200, formula_script(), "text/javascript")
            elif self.path == "/workspace-link.js":
                self.reply(200, workspace_link_script(), "text/javascript")
            elif self.path == "/court.jpg":
                self.reply(200, court_image(), "image/jpeg")
            elif self.path == "/api/bootstrap":
                self.reply(
                    200,
                    json.dumps(
                        self.local.jobs.bootstrap(), ensure_ascii=False, allow_nan=False
                    ).encode(),
                )
            elif self.path == "/api/job":
                self.reply(
                    200,
                    json.dumps(
                        self.local.jobs.read(), ensure_ascii=False, allow_nan=False
                    ).encode(),
                )
            else:
                path = urlsplit(self.path).path
                assets = {
                    "/": ("index.html", "text/html"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/views.js": ("views.js", "text/javascript"),
                    "/forms.js": ("forms.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }
                if path not in assets:
                    self.reply(404, b'{"error":"route not found"}')
                    return
                name, media = assets[path]
                self.reply(200, (Path(__file__).parent / "static" / name).read_bytes(), media)
        except (DataError, ValueError) as exc:
            self.reply(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())

    def do_POST(self) -> None:
        if not self.allowed(True):
            return
        try:
            self.connection.settimeout(self.local.session.params.request_timeout.value)
            if (
                self.headers.get("Content-Type") != "application/json"
                or self.headers.get("Transfer-Encoding") is not None
            ):
                raise DataError("request: application/json and Content-Length are required")
            raw = self.headers.get("Content-Length", "")
            if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) <= 2_000_000:
                raise DataError("request: invalid or excessive body length")
            data = self.rfile.read(int(raw))
            if len(data) != int(raw):
                raise DataError("request: incomplete body")
            if self.path == "/api/quit":
                self.reply(200, b'{"stopping":true}')
                self.local.stopping.set()
            elif self.path == "/api/action":
                action, body = parse_body(data)
                job = self.local.jobs.start(action, body)
                self.reply(202, json.dumps({"job": job}).encode())
            else:
                self.reply(404, b'{"error":"route not found"}')
        except (DataError, ValueError, TimeoutError) as exc:
            self.reply(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())


def serve_inseason(root: Path, *, open_browser: bool = True) -> None:
    lock = InstanceLock(root / "inseason.lock")
    if not lock.acquire():
        open_existing(lock, "inseason")
        return
    try:
        session = InseasonSession(root, Path(__file__).parent / "defaults")
        with SeasonServer(session) as server, shutdown_signals(server.stopping):
            lock.publish(server.instance)
            launch_browser(server, open_browser=open_browser)
            try:
                server.run_scheduled()
            finally:
                server.jobs.stop(session.preferences.shutdown_seconds)
    finally:
        lock.release()
