import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from fba.apps.workspace.modes import ModeManager, OpenMode, mode_url
from fba.contracts.base import DataError
from fba.data.codec import canonical, decode
from fba.runtime.assets import court_image
from fba.runtime.local import Instance, LocalServer, permitted


class WorkspaceServer(LocalServer):
    def __init__(self, modes: ModeManager) -> None:
        self.modes = modes
        super().__init__(0, WorkspaceHandler)
        self.instance = Instance(
            app="workspace",
            pid=os.getpid(),
            port=self.server_port,
            token=self.token,
            started_at=self.started_at,
        )


class WorkspaceHandler(BaseHTTPRequestHandler):
    @property
    def local(self) -> WorkspaceServer:
        assert isinstance(self.server, WorkspaceServer)
        return self.server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def reply(self, status: int, payload: bytes, media: str = "application/json") -> None:
        self.send_response(status)
        for key, value in {
            "Content-Type": media,
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; connect-src 'self'; script-src 'self'; "
                "style-src 'self'; img-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
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
        if self.path == "/health":
            self.reply(200, canonical(self.local.instance))
        elif self.path == "/api/bootstrap":
            self.reply(200, canonical(self.local.modes.settings))
        elif self.path == "/court.jpg":
            self.reply(200, court_image(), "image/jpeg")
        else:
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            entry = assets.get(urlsplit(self.path).path)
            if entry is None:
                self.reply(404, b'{"error":"route not found"}')
                return
            name, media = entry
            self.reply(200, (Path(__file__).parent / "static" / name).read_bytes(), media)

    def do_POST(self) -> None:
        if not self.allowed(True):
            return
        try:
            length = self.headers.get("Content-Length", "")
            if (
                self.headers.get("Content-Type") != "application/json"
                or self.headers.get("Transfer-Encoding") is not None
                or not length.isascii()
                or not length.isdecimal()
                or not 0 < int(length) <= 16_384
            ):
                raise DataError("request: requires application/json and a bounded Content-Length")
            data = self.rfile.read(int(length))
            if len(data) != int(length):
                raise DataError("request: incomplete body")
            if self.path == "/api/open":
                request = decode(OpenMode, data, "workspace.open")
                url = mode_url(self.local.modes.open(request), self.local.origin)
                self.reply(200, json.dumps({"url": url}).encode())
            elif self.path == "/api/quit":
                self.reply(200, b'{"stopping":true}')
                self.local.stopping.set()
            else:
                self.reply(404, b'{"error":"route not found"}')
        except (DataError, ValueError, OSError, TimeoutError) as exc:
            self.reply(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())
