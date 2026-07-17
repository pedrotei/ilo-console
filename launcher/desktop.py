"""Native launcher: a saved-server list you add/edit/remove entries in, with
a "Connect" button per entry that opens that server's console.

Deliberately doesn't embed the console itself -- "Connect" just launches the
existing `ilo3console.desktop`/`ilo100console.desktop` native app as its own
subprocess/window, pre-filled with the saved host/username. If a password is
saved for that server (OS keyring, see store.py), it's handed to the child
process via an environment variable rather than a CLI argument (CLI args are
visible to other processes on the same machine via ``ps``; env vars aren't,
short of reading that process's own ``/proc/<pid>/environ``) -- that app then
auto-submits its own connect dialog. No password is ever written to disk or
put on a command line.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from . import certmonitor, renewal, store

try:
    import webview
except ImportError:  # pragma: no cover - optional dependency
    webview = None

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_MODULE_BY_GENERATION = {
    "ilo3": "ilo3console.desktop",
    "ilo100": "ilo100console.desktop",
}


class Api:
    """Exposed to the webview's JavaScript as ``window.pywebview.api``."""

    def list_servers(self) -> list[dict]:
        """Return all saved servers."""
        return store.load_servers()

    def add_server(self, fields: dict) -> dict:
        """Validate and save a new server entry. Raises on invalid input,
        which pywebview surfaces to JS as a rejected promise."""
        return store.add_server(fields)

    def update_server(self, server_id: str, fields: dict) -> dict:
        """Validate and overwrite an existing server entry."""
        return store.update_server(server_id, fields)

    def remove_server(self, server_id: str) -> bool:
        """Delete a saved server entry."""
        return store.remove_server(server_id)

    def reorder_servers(self, ordered_ids: list[str]) -> list[dict]:
        """Persist a new display order for the saved servers."""
        return store.reorder_servers(ordered_ids)

    def get_cert_statuses(self) -> dict:
        """Return the latest cached iLO 3 certificate-check results, keyed
        by server id. Background thread refreshes this once a minute."""
        return certmonitor.get_statuses()

    def get_acme_settings(self) -> dict:
        """Return non-secret ACME settings (contact email) plus whether a
        Cloudflare API token is currently saved."""
        settings = store.get_acme_settings()
        settings["has_cloudflare_token"] = bool(store.get_cloudflare_token())
        return settings

    def save_acme_settings(self, fields: dict) -> dict:
        """Persist non-secret ACME settings (contact email)."""
        return store.save_acme_settings(fields)

    def save_cloudflare_token(self, token: str) -> dict:
        """Save the Cloudflare API token used for ACME DNS-01 challenges."""
        store.set_cloudflare_token(token)
        return {"ok": True}

    def start_renewal(self, server_id: str, password: str | None = None) -> dict:
        """Kick off a certificate-renewal job for a saved iLO 3 server."""
        try:
            job_id = renewal.start_renewal(server_id, password)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job_id": job_id}

    def get_renewal_status(self, job_id: str) -> dict:
        """Poll a renewal job's latest status."""
        status = renewal.get_job_status(job_id)
        return status if status is not None else {"error": "unknown job"}

    def launch(self, server_id: str) -> dict:
        """Open the given server's console in its own native window.

        Spawns the existing per-generation desktop app as a detached
        subprocess, pre-filled with the saved host/username. If a password
        is saved (OS keyring) for this server, it's passed via an env var so
        that app auto-connects instead of waiting on its own dialog.
        """
        server = store.get_server(server_id)
        if server is None:
            return {"ok": False, "error": "server not found"}
        module = _MODULE_BY_GENERATION[server["generation"]]
        argv = [sys.executable, "-m", module, "--host", server["host"]]
        if server.get("username"):
            argv += ["--user", server["username"]]
        env = dict(os.environ)
        password = store.get_password(server_id)
        if password:
            env["ILO_CONSOLE_PASSWORD"] = password
        try:
            # Deliberately detached: this window outlives the launcher, no
            # pipes to manage, so there's nothing for a `with` block to clean up.
            subprocess.Popen(argv, cwd=_REPO_ROOT, env=env)  # pylint: disable=consider-using-with
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}


def main() -> None:
    """Open the native launcher window."""
    # Errors (e.g. a failed Cloudflare API call during certificate renewal)
    # always print; set ILO_CONSOLE_DEBUG=1 for per-request detail too.
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("ILO_CONSOLE_DEBUG") else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if webview is None:
        raise SystemExit(
            "pywebview is not installed. Run: pip install pywebview\n"
            "(Linux also needs a WebKitGTK or Qt WebEngine backend.)"
        )
    certmonitor.start()
    webview.create_window(
        "iLO Console Launcher",
        os.path.join(_WEB_DIR, "index.html"),
        js_api=Api(),
        width=760,
        height=640,
        min_size=(560, 420),
        background_color="#0b0e10",
    )
    webview.start()


if __name__ == "__main__":
    main()
