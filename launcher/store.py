"""Saved-server list: add/edit/remove/list, persisted as local JSON.

Server records (host/username/port/ssl/generation) are stored in plain JSON.
Passwords are never written there -- if a password is saved, it goes to the
OS keyring (Keychain/Secret Service/Credential Manager, via the ``keyring``
package) under the service ``ilo-console``, keyed by the server's id. The
JSON record only carries a ``has_saved_password`` flag so the UI knows
whether one exists, never the password itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError:  # pragma: no cover - optional dependency
    keyring = None

GENERATIONS = ("ilo3", "ilo100")
_KEYRING_SERVICE = "ilo-console"


def _require_keyring() -> Any:
    if keyring is None:
        raise RuntimeError(
            "keyring is not installed. Run: pip install keyring "
            "(or install the 'launcher' extra)."
        )
    return keyring


def get_password(server_id: str) -> str | None:
    """Return the saved password for a server, or None if none is saved."""
    if keyring is None:
        return None
    return keyring.get_password(_KEYRING_SERVICE, server_id)


def _set_password(server_id: str, password: str) -> None:
    _require_keyring().set_password(_KEYRING_SERVICE, server_id, password)


def _delete_password(server_id: str) -> None:
    if keyring is None:
        return
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, server_id)


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ilo-console"


def _store_path() -> Path:
    return _config_dir() / "servers.json"


def _validate(fields: dict[str, Any]) -> dict[str, Any]:
    name = str(fields.get("name") or "").strip()
    host = str(fields.get("host") or "").strip()
    generation = fields.get("generation")
    if not name:
        raise ValueError("name is required")
    if not host:
        raise ValueError("host is required")
    if generation not in GENERATIONS:
        raise ValueError(f"generation must be one of {GENERATIONS}")
    username = str(fields.get("username") or "").strip()
    port = fields.get("port")
    port = int(port) if port not in (None, "") else None
    ssl = bool(fields.get("ssl", False))
    return {
        "name": name,
        "host": host,
        "username": username,
        "port": port,
        "ssl": ssl,
        "generation": generation,
    }


def load_servers() -> list[dict[str, Any]]:
    """Return all saved servers, oldest first. Missing/corrupt store -> []."""
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("servers", []) if isinstance(data, dict) else []


def _save_servers(servers: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"servers": servers}, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_server(server_id: str) -> dict[str, Any] | None:
    """Look up a single saved server by id, or None if it doesn't exist."""
    for server in load_servers():
        if server.get("id") == server_id:
            return server
    return None


def _apply_password(server: dict[str, Any], server_id: str, fields: dict[str, Any]) -> None:
    """Save/clear the keyring password for ``server_id`` per ``fields``, and
    set ``server["has_saved_password"]`` to match.

    ``fields["save_password"]`` false/absent -> clear any saved password.
    True with a non-empty ``fields["password"]`` -> save/overwrite it.
    True with an empty password -> leave whatever's already saved alone
    (lets an edit that doesn't touch the password field keep it).
    """
    password = fields.get("password") or ""
    if not fields.get("save_password"):
        _delete_password(server_id)
        server["has_saved_password"] = False
    elif password:
        _set_password(server_id, password)
        server["has_saved_password"] = True
    else:
        server["has_saved_password"] = get_password(server_id) is not None


def add_server(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and append a new server entry, assigning it a fresh id."""
    server = _validate(fields)
    server["id"] = uuid.uuid4().hex
    _apply_password(server, server["id"], fields)
    servers = load_servers()
    servers.append(server)
    _save_servers(servers)
    return server


def update_server(server_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and overwrite an existing server entry's fields (id unchanged)."""
    updated = _validate(fields)
    updated["id"] = server_id
    _apply_password(updated, server_id, fields)
    servers = load_servers()
    for i, server in enumerate(servers):
        if server.get("id") == server_id:
            servers[i] = updated
            _save_servers(servers)
            return updated
    raise KeyError(f"no server with id {server_id!r}")


def remove_server(server_id: str) -> bool:
    """Delete a server entry by id, and any password saved for it."""
    servers = load_servers()
    kept = [s for s in servers if s.get("id") != server_id]
    if len(kept) == len(servers):
        return False
    _delete_password(server_id)
    _save_servers(kept)
    return True


def reorder_servers(ordered_ids: list[str]) -> list[dict[str, Any]]:
    """Persist a new display order for the saved servers.

    ``ordered_ids`` must be a permutation of the current servers' ids
    (every existing id present, exactly once, nothing extra) -- this is a
    full reorder, not a partial move, so a stale/incomplete list from a
    caller can't silently drop entries.
    """
    servers = load_servers()
    by_id = {s["id"]: s for s in servers}
    if set(ordered_ids) != set(by_id):
        raise ValueError("ordered_ids must match the current set of server ids exactly")
    reordered = [by_id[server_id] for server_id in ordered_ids]
    _save_servers(reordered)
    return reordered
