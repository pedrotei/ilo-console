"""Native launcher: a saved-server list you add/edit/remove entries in, with
a "Connect" button per entry that opens that server's console.

Deliberately doesn't embed the console itself -- "Connect" just launches the
existing, already-validated `ilo3console.desktop`/`ilo100console.desktop`
native app as its own subprocess/window, pre-filled with the saved
host/username (password is always typed fresh; see store.py). This reuses
those apps completely unmodified.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import store

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

    def launch(self, server_id: str) -> dict:
        """Open the given server's console in its own native window.

        Spawns the existing per-generation desktop app as a detached
        subprocess, pre-filled with the saved host/username; the password is
        always typed fresh in that app's own connect dialog.
        """
        server = store.get_server(server_id)
        if server is None:
            return {"ok": False, "error": "server not found"}
        module = _MODULE_BY_GENERATION[server["generation"]]
        argv = [sys.executable, "-m", module, "--host", server["host"]]
        if server.get("username"):
            argv += ["--user", server["username"]]
        try:
            # Deliberately detached: this window outlives the launcher, no
            # pipes to manage, so there's nothing for a `with` block to clean up.
            subprocess.Popen(argv, cwd=_REPO_ROOT)  # pylint: disable=consider-using-with
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}


def main() -> None:
    """Open the native launcher window."""
    if webview is None:
        raise SystemExit(
            "pywebview is not installed. Run: pip install pywebview\n"
            "(Linux also needs a WebKitGTK or Qt WebEngine backend.)"
        )
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
