"""Saved-server list: add/edit/remove/list, persisted as local JSON.

Deliberately stores host/username/port/ssl/generation only -- never a
password (see the "Connect" flow in desktop.py/bridge.py, which always
prompts for it fresh via the existing per-generation dialog).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

GENERATIONS = ("ilo3", "ilo100")


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


def add_server(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and append a new server entry, assigning it a fresh id."""
    server = _validate(fields)
    server["id"] = uuid.uuid4().hex
    servers = load_servers()
    servers.append(server)
    _save_servers(servers)
    return server


def update_server(server_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and overwrite an existing server entry's fields (id unchanged)."""
    updated = _validate(fields)
    updated["id"] = server_id
    servers = load_servers()
    for i, server in enumerate(servers):
        if server.get("id") == server_id:
            servers[i] = updated
            _save_servers(servers)
            return updated
    raise KeyError(f"no server with id {server_id!r}")


def remove_server(server_id: str) -> bool:
    """Delete a server entry by id. Returns whether anything was removed."""
    servers = load_servers()
    kept = [s for s in servers if s.get("id") != server_id]
    if len(kept) == len(servers):
        return False
    _save_servers(kept)
    return True
