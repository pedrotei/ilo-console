"""Minimal Cloudflare DNS client: just enough to create/delete the
``_acme-challenge`` TXT record an ACME DNS-01 challenge needs (see
``ilo3console/acme.py``/``ilo3console/certrenew.py``).

Plain ``urllib`` against Cloudflare's REST API v4, same stdlib-first style as
the rest of this project.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

_API_BASE = "https://api.cloudflare.com/client/v4"
_log = logging.getLogger(__name__)


class CloudflareError(Exception):
    """Raised on any non-success Cloudflare API response."""


def _clean_token(api_token: str) -> str:
    """Strip whitespace, surrounding quotes, and an accidental "Bearer "
    prefix -- all common artifacts of copy-pasting a token into a UI field or
    a shell variable, any of which corrupts the ``Authorization`` header just
    like a raw formatting mistake would."""
    token = api_token.strip().strip("'\"").strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return token


def _redact(token: str) -> str:
    if len(token) <= 10:
        return "<redacted>"
    return f"{token[:6]}...{token[-4:]} (len={len(token)})"


def _request(api_token: str, method: str, path: str, payload: dict | None = None) -> dict:
    token = _clean_token(api_token)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        # Cloudflare's API rejects GET/DELETE requests that carry a
        # Content-Type header with no body ("Invalid request headers"),
        # so this is only set when there's an actual JSON payload to send.
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_API_BASE}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    _log.debug("Cloudflare request: %s %s (token %s)", method, path, _redact(token))
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        _log.error(
            "Cloudflare %s %s -> HTTP %s: %s",
            method,
            path,
            exc.code,
            raw.decode("utf-8", "replace"),
        )
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CloudflareError(f"Cloudflare API HTTP {exc.code}: {exc.reason}") from exc
        raise CloudflareError(f"Cloudflare API error: {_format_errors(body)}") from exc
    if not body.get("success"):
        _log.error("Cloudflare %s %s -> success=false: %s", method, path, body)
        raise CloudflareError(f"Cloudflare API error: {_format_errors(body)}")
    return body


def _format_errors(body: dict) -> str:
    errors = body.get("errors") or []
    parts = []
    for error in errors:
        message = str(error.get("message", error))
        code = error.get("code")
        if code is not None:
            message = f"{message} (code {code})"
        # The top-level message is often a generic wrapper (e.g. "Invalid
        # request headers" for code 6003); the actual cause is nested here.
        chain = error.get("error_chain") or []
        if chain:
            nested = "; ".join(str(c.get("message", c)) for c in chain)
            message = f"{message}: {nested}"
        parts.append(message)
    return "; ".join(parts) or "unknown error"


def find_zone_id(api_token: str, hostname: str) -> str:
    """Find the Cloudflare zone that owns ``hostname``.

    Walks ``hostname``'s parent domains (most-specific first) since the
    hostname itself is usually a subdomain, not the registered zone.
    """
    labels = hostname.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        body = _request(api_token, "GET", f"/zones?name={urllib.parse.quote(candidate)}")
        result = body.get("result") or []
        if result:
            return result[0]["id"]
    raise CloudflareError(f"no Cloudflare zone found for any parent domain of {hostname}")


def create_txt_record(api_token: str, zone_id: str, name: str, value: str) -> str:
    """Create a TXT record; returns its record id (for later deletion)."""
    body = _request(
        api_token,
        "POST",
        f"/zones/{zone_id}/dns_records",
        {"type": "TXT", "name": name, "content": value, "ttl": 120},
    )
    return body["result"]["id"]


def delete_txt_record(api_token: str, zone_id: str, record_id: str) -> None:
    """Delete a previously-created TXT record."""
    _request(api_token, "DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
