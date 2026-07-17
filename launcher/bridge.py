"""Browser launcher: serves the saved-server list (add/edit/remove) as a
small local web page, with a "Connect" button per entry.

Deliberately doesn't proxy the console traffic itself -- "Connect" ensures
the existing, already-validated `ilo3console.bridge`/`ilo100console.bridge`
WebSocket bridge is running (as its own subprocess, on its own default port,
completely unmodified) and hands back its URL, pre-filled with the saved
host/username via the query-string prefill both of those front-ends already
support. The browser tab then navigates there directly.

Pure standard library (``http.server``) -- this page is just local CRUD +
process supervision, not console traffic, so it doesn't need ``websockets``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import certmonitor, renewal, store

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# (module, default port) -- matches each bridge's own --listen default.
_BRIDGE_BY_GENERATION = {
    "ilo3": ("ilo3console.bridge", 8080),
    "ilo100": ("ilo100console.bridge", 8081),
}

def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_bridge_running(generation: str, bind_host: str) -> int:
    """Start the per-generation bridge if it isn't already listening;
    return its port."""
    module, port = _BRIDGE_BY_GENERATION[generation]
    if _port_open(bind_host, port):
        return port
    # Deliberately detached: this process outlives the launcher, no pipes
    # to manage, so there's nothing for a `with` block to clean up.
    subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-m", module, "--listen", f"{bind_host}:{port}"],
        cwd=_REPO_ROOT,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _port_open(bind_host, port):
            break
        time.sleep(0.1)
    return port


class _Handler(BaseHTTPRequestHandler):
    """Serves the launcher's static assets, a small JSON CRUD API for the
    saved-server list, and a launch endpoint that starts (or reuses) the
    target per-generation bridge subprocess."""

    server_version = "ilo-console-launcher/1"
    bind_host = "127.0.0.1"  # overridden by run() before the server starts

    def log_message(  # noqa: A002 pylint: disable=redefined-builtin
        self, format, *args
    ) -> None:
        """Suppress the default per-request stderr log line; connection
        noise here isn't actionable."""

    # -- static assets --------------------------------------------------

    def _serve_file(self, name: str, content_type: str) -> None:
        try:
            with open(os.path.join(_WEB_DIR, name), "rb") as fh:
                body = fh.read()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- JSON helpers -----------------------------------------------------

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    # -- routing --------------------------------------------------------
    # pylint: disable=invalid-name
    # (do_GET/do_POST/do_PUT/do_DELETE names are stdlib-mandated, not ours to pick)

    def do_GET(self) -> None:
        """Serve static assets and the read-only server-list endpoint."""
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._serve_file("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/servers":
            self._json(HTTPStatus.OK, store.load_servers())
        elif path == "/api/cert-status":
            self._json(HTTPStatus.OK, certmonitor.get_statuses())
        elif path == "/api/acme-settings":
            settings = store.get_acme_settings()
            settings["has_cloudflare_token"] = bool(store.get_cloudflare_token())
            self._json(HTTPStatus.OK, settings)
        elif path.startswith("/api/renewal-status/"):
            job_id = path.removeprefix("/api/renewal-status/")
            status = renewal.get_job_status(job_id)
            if status is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
                return
            self._json(HTTPStatus.OK, status)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        """Handle server creation and console-launch requests."""
        path = urlparse(self.path).path
        if path == "/api/servers":
            try:
                created = store.add_server(self._read_json_body())
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, created)
        elif path.startswith("/api/launch/"):
            server_id = path.removeprefix("/api/launch/")
            server = store.get_server(server_id)
            if server is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "server not found"})
                return
            port = _ensure_bridge_running(server["generation"], self.bind_host)
            if not _port_open(self.bind_host, port, timeout=1.0):
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "console bridge did not start in time"},
                )
                return
            host = urlparse(f"//{self.headers.get('Host', self.bind_host)}").hostname
            qs = f"host={server['host']}"
            if server.get("username"):
                qs += f"&user={server['username']}"
            self._json(HTTPStatus.OK, {"url": f"http://{host}:{port}/?{qs}"})
        elif path == "/api/acme-settings":
            self._json(HTTPStatus.OK, store.save_acme_settings(self._read_json_body()))
        elif path == "/api/cloudflare-token":
            token = str(self._read_json_body().get("token") or "")
            if not token:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "token is required"})
                return
            store.set_cloudflare_token(token)
            self._json(HTTPStatus.OK, {"ok": True})
        elif path.startswith("/api/renew/"):
            server_id = path.removeprefix("/api/renew/")
            password = self._read_json_body().get("password") or None
            try:
                job_id = renewal.start_renewal(server_id, password)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"job_id": job_id})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self) -> None:
        """Handle server-edit and reorder requests."""
        path = urlparse(self.path).path
        if path == "/api/servers/order":
            try:
                reordered = store.reorder_servers(self._read_json_body().get("order", []))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, reordered)
        elif path.startswith("/api/servers/"):
            server_id = path.removeprefix("/api/servers/")
            try:
                updated = store.update_server(server_id, self._read_json_body())
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, updated)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_DELETE(self) -> None:
        """Handle server-removal requests."""
        path = urlparse(self.path).path
        if path.startswith("/api/servers/"):
            server_id = path.removeprefix("/api/servers/")
            removed = store.remove_server(server_id)
            self._json(HTTPStatus.OK if removed else HTTPStatus.NOT_FOUND, {"ok": removed})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def run(host: str, port: int) -> None:
    """Serve the launcher page on ``host``:``port`` until interrupted."""
    _Handler.bind_host = host
    certmonitor.start()
    with ThreadingHTTPServer((host, port), _Handler) as httpd:
        print(f"iLO console launcher on http://{host}:{port}/")
        httpd.serve_forever()


def main() -> None:
    """Parse CLI args and run the launcher's local web server."""
    # Errors (e.g. a failed Cloudflare API call during certificate renewal)
    # always print; set ILO_CONSOLE_DEBUG=1 for per-request detail too.
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("ILO_CONSOLE_DEBUG") else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="iLO console launcher (server list)")
    parser.add_argument(
        "--listen", default="127.0.0.1:8079", help="host:port to listen on"
    )
    args = parser.parse_args()
    host, _, port = args.listen.rpartition(":")
    run(host or "127.0.0.1", int(port))


if __name__ == "__main__":
    main()
