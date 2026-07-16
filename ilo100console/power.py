"""Chassis power control for iLO 100 — a separate channel from the Mahogany
KVM socket entirely.

Unlike iLO 3 (which has a dedicated power-control message on its console
channel), the Mahogany protocol has no power messages at all (see
docs/PROTOCOL_ILO100.md — the only related message is ``StandbyPower``,
which is a *display* standby/blank state, not chassis power). The real
power control lives in the plain HTML/CGI-style web management UI: ``GET
/chassis.html`` (Digest auth,
same as ``session.fetch_httpdata_token``) renders a "Virtual Power" page with
an HTML ``<FORM METHOD="get" ACTION="chassis.html">`` whose ``PwrCtrl``
query param drives the action, and the response re-renders the same page
with the ``Power Status:`` field reflecting the new state.
"""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

# This BMC's embedded httpd is old and easy to overload: it can reset a
# connection mid-response (raw ConnectionResetError, not even wrapped as a
# clean HTTPError/URLError) even for a bare unauthenticated request, and
# recovers anywhere from under a second to tens of seconds later (confirmed
# live both ways). Retry-with-backoff turns that transient blip into a
# non-event instead of a hard failure -- this is read-only, so always safe.
_TRANSIENT_RETRY_DELAYS = (0.5, 1, 2, 3, 5, 5)

POWER_UP = "PowerUp"
POWER_CYCLE = "PowerCycle"
SOFT_POWER_OFF = "SoftPwrDn"
HARD_RESET = "HardReset"
POWER_DOWN = "PowerDown"
FORCE_DUMP = "ForceDump"

_VALID_ACTIONS = {
    POWER_UP, POWER_CYCLE, SOFT_POWER_OFF, HARD_RESET, POWER_DOWN, FORCE_DUMP,
}

_STATUS_RE = re.compile(
    r"Power Status:</STRONG></TD><TD>(ON|OFF)</TD>", re.IGNORECASE
)


class PowerControlError(Exception):
    """Raised when a chassis power request or status fetch fails."""


class _TransientConnectionError(Exception):
    """Internal: a raw reset worth retrying, as opposed to a real auth/URL
    failure (wrong password, unreachable host) which should fail fast."""


def _open_chassis_html(
    host: str,
    username: str,
    password: str,
    *,
    web_port: int | None = None,
    use_ssl: bool = False,
    timeout: float = 15.0,
    query: str = "",
) -> str:
    port = web_port if web_port is not None else (443 if use_ssl else 80)
    scheme = "https" if use_ssl else "http"
    default_port = 443 if use_ssl else 80
    suffix = "" if port == default_port else f":{port}"
    url = f"{scheme}://{host}{suffix}/chassis.html"
    if query:
        url += f"?{query}"

    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, url, username, password)
    handlers = [urllib.request.HTTPDigestAuthHandler(pwd_mgr)]
    if use_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.read().decode("latin-1")
    except urllib.error.HTTPError as exc:
        raise PowerControlError(
            f"chassis.html request to {host} failed: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PowerControlError(f"cannot reach {host}: {exc.reason}") from exc
    except ConnectionError as exc:
        # Raw (unwrapped-by-urllib) reset -- see module docstring / the retry
        # helper in get_power_status, which is what actually retries this.
        raise _TransientConnectionError(str(exc)) from exc


def _parse_status(html: str) -> str:
    match = _STATUS_RE.search(html)
    if not match:
        raise PowerControlError(
            "could not find Power Status field in chassis.html response"
        )
    return match.group(1).upper()


def get_power_status(
    host: str,
    username: str,
    password: str,
    *,
    web_port: int | None = None,
    use_ssl: bool = False,
    timeout: float = 15.0,
) -> str:
    """Returns ``"ON"`` or ``"OFF"``.

    Retries on a transient reset -- this is a read-only GET, so retrying is
    always safe (unlike ``send_power_action``, which is not idempotent).
    """
    last_exc: _TransientConnectionError | None = None
    for delay in (0.0,) + _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            html = _open_chassis_html(
                host, username, password,
                web_port=web_port, use_ssl=use_ssl, timeout=timeout,
            )
            return _parse_status(html)
        except _TransientConnectionError as exc:
            last_exc = exc
    raise PowerControlError(
        f"connection to {host} was reset repeatedly: {last_exc}"
    ) from last_exc


def send_power_action(
    host: str,
    username: str,
    password: str,
    action: str,
    *,
    web_port: int | None = None,
    use_ssl: bool = False,
    timeout: float = 15.0,
) -> str:
    """Submits the chassis power form (``PwrCtrl=<action>&Button1=Apply``) and
    returns the resulting ``Power Status`` ("ON"/"OFF") from the response.
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"unknown power action {action!r}")
    query = urllib.parse.urlencode({"PwrCtrl": action, "Button1": "Apply"})
    try:
        html = _open_chassis_html(
            host, username, password,
            web_port=web_port, use_ssl=use_ssl, timeout=timeout, query=query,
        )
    except _TransientConnectionError as exc:
        # Deliberately not retried here -- this GET isn't idempotent, and the
        # action may have already landed even though we lost the response
        # (see client.ILo100Client.send_power_action's status-recheck
        # fallback, which is what actually handles this case).
        raise PowerControlError(f"connection to {host} was reset: {exc}") from exc
    return _parse_status(html)
