"""Background periodic TLS-certificate freshness checks for saved iLO 3 servers.

Runs once a minute in a daemon thread, shared by both launcher backends
(desktop.py, bridge.py). A single TLS handshake to each iLO 3 server's HTTPS
port doubles as both the "is it alive" ping and the certificate fetch --
deliberately not two separate connections per check, since this hits real
embedded management firmware every minute for as long as the launcher runs
(see docs/PROTOCOL_ILO3.md's live-testing notes on iLO 3 needing a power-cycle
after heavy connection load).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import store

CHECK_INTERVAL_SECONDS = 60
_CONNECT_TIMEOUT = 5.0

_lock = threading.Lock()
_statuses: dict[str, dict[str, Any]] = {}
_start_lock = threading.Lock()
_started_event = threading.Event()


def _check_one(server: dict[str, Any]) -> dict[str, Any]:
    from ilo3console.certcheck import fetch_certificate  # pylint:disable=import-outside-toplevel

    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        info = fetch_certificate(
            server["host"], server.get("port") or 443, timeout=_CONNECT_TIMEOUT
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Any failure here (connection refused/timed out, TLS handshake
        # failure, cryptography not installed, ...) just means "can't tell
        # right now" -- surfaced to the UI as unreachable, not a crash.
        return {"reachable": False, "error": str(exc), "checked_at": checked_at}
    return {
        "reachable": True,
        "checked_at": checked_at,
        "subject": info.subject,
        "issuer": info.issuer,
        "self_signed": info.is_self_signed,
        "not_before": info.not_before.isoformat(),
        "not_after": info.not_after.isoformat(),
        "days_remaining": info.days_remaining,
        "expired": info.expired,
        "not_yet_valid": info.not_yet_valid,
        "invalid": info.invalid,
    }


def get_statuses() -> dict[str, dict[str, Any]]:
    """Return the most recently cached status, keyed by server id.

    Non-blocking -- always returns whatever the background thread last
    found, rather than triggering a fresh network round-trip.
    """
    with _lock:
        return dict(_statuses)


def _run_once() -> None:
    for server in store.load_servers():
        if server.get("generation") != "ilo3":
            continue
        result = _check_one(server)
        with _lock:
            _statuses[server["id"]] = result


def check_now(server_id: str) -> None:
    """Refresh one server's cached status immediately, out of band from the
    once-a-minute loop -- used right after a certificate renewal so the UI
    doesn't have to wait up to 60s to see the result."""
    server = store.get_server(server_id)
    if server is None or server.get("generation") != "ilo3":
        return
    result = _check_one(server)
    with _lock:
        _statuses[server_id] = result


def _loop() -> None:
    while True:
        _run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


def start() -> None:
    """Start the background checker thread. Idempotent -- safe to call
    more than once (e.g. once per Api instance)."""
    with _start_lock:
        if _started_event.is_set():
            return
        _started_event.set()
        threading.Thread(target=_loop, daemon=True).start()
