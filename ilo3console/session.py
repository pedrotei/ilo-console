"""HTTPS session + JSON layer for iLO 3.

The iLO's own web-based console client is handed a ready-made session token
(``RCINFO1``) by the iLO web UI. A standalone client has no web UI, so this
module logs in with a username and password to mint the token itself, then
exposes the same ``/json/*`` endpoints (`rc_info`, `session_info`, `vm_status`).

You can also skip the login entirely and pass a token you already have
(``ILoSession.from_token``) — useful for tests or when a token was captured
elsewhere.

iLO 3 firmware only speaks legacy TLS (TLSv1.0, legacy renegotiation, low
security level) with a self-signed certificate, so this builds a deliberately
permissive SSL context. See ``openssl-legacy.cnf`` at the repo root for the
equivalent OpenSSL knobs.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any


def legacy_tls_context() -> ssl.SSLContext:
    """SSL context that can talk to iLO 3's ancient TLS stack.

    Disables certificate verification (self-signed iLO cert), allows unsafe
    legacy renegotiation, drops the minimum protocol to TLSv1.0, and lowers the
    cipher security level so OpenSSL 3 will negotiate the old suites iLO offers.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Allow TLSv1.0 (Python >=3.10 gates this behind minimum_version).
    with contextlib.suppress(ValueError, AttributeError):
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    # Permit servers that don't advertise RFC 5746 secure renegotiation.
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    # SECLEVEL=0 re-enables the weak cipher suites iLO 3 relies on.
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        ctx.set_ciphers("ALL")
    return ctx


@dataclass
class RcInfo:
    """Parsed ``/json/rc_info`` — the console connection parameters."""

    session_key: str
    enc_key: str | None
    enc_key_val: bytes
    rc_port: int
    vm_key: str | None
    vm_port: int | None
    optional_features: str
    server_name: str | None
    ilo_fqdn: str | None
    raw: dict

    @property
    def encrypt_key(self) -> bool:
        """Whether the handshake token must be XOR-obfuscated with ``enc_key``."""
        return "ENCRYPT_KEY" in self.optional_features

    @property
    def encrypt_vmkey(self) -> bool:
        """Whether the virtual-media handshake token must be XOR-obfuscated."""
        return "ENCRYPT_VMKEY" in self.optional_features


class ILoSession:
    """An authenticated HTTPS session against one iLO.

    Construct via :meth:`login` (username/password) or :meth:`from_token`
    (pre-minted token). The ``session_key`` attribute is the token the console
    and command channels authenticate with.
    """

    def __init__(self, host: str, port: int = 443, *, timeout: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.session_key: str | None = None
        self._ctx = legacy_tls_context()
        self._cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            urllib.request.HTTPCookieProcessor(self._cookies),
        )

    @property
    def base_url(self) -> str:
        """The ``https://host[:port]`` prefix for JSON endpoint requests."""
        suffix = "" if self.port == 443 else f":{self.port}"
        return f"https://{self.host}{suffix}"

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_token(
        cls, host: str, session_key: str, port: int = 443, **kw: Any
    ) -> ILoSession:
        """Build a session from an already-minted token, skipping login."""
        self = cls(host, port, **kw)
        self.session_key = session_key
        return self

    @classmethod
    def login(
        cls,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        **kw: Any,
    ) -> ILoSession:
        """Log in with credentials and mint a session token.

        iLO's JSON login endpoint accepts ``{"method":"login", ...}`` and
        returns a ``session_key`` (also set as the ``sessionKey`` cookie). Raises
        :class:`AuthError` if the credentials are rejected.
        """
        self = cls(host, port, **kw)
        self._do_login(username, password)
        return self

    def _do_login(self, username: str, password: str) -> None:
        body = json.dumps(
            {"method": "login", "user_login": username, "password": password}
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/json/login_session",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise AuthError(
                f"login rejected by {self.host}: HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"cannot reach {self.host}: {exc.reason}") from exc

        token = self._extract_session_key(text)
        if not token:
            raise AuthError(
                f"login to {self.host} did not yield a session key "
                f"(response: {text[:200]!r})"
            )
        self.session_key = token

    def _extract_session_key(self, text: str) -> str | None:
        # Prefer the cookie the server set; fall back to the JSON body.
        for cookie in self._cookies:
            if cookie.name == "sessionKey" and cookie.value:
                return cookie.value
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        for key in ("session_key", "sessionKey", "session"):
            if isinstance(data.get(key), str):
                return data[key]
        return None

    # -- JSON requests --------------------------------------------------------

    def _request(
        self, name: str, method: str = "GET", body: str | None = None
    ) -> str:
        if not self.session_key:
            raise RuntimeError("no session key; call login() or from_token()")
        headers = {"Cookie": f"sessionKey={self.session_key}"}
        data = None
        if body is not None:
            data = body.encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}/json/{name}", data=data, headers=headers, method=method
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def get_json(self, name: str) -> Any:
        """GET ``/json/{name}`` and parse the response body as JSON."""
        return json.loads(self._request(name))

    def post_json(self, name: str, payload: dict) -> str:
        """POST ``payload`` as JSON to ``/json/{name}`` and return the raw
        response text.
        """
        return self._request(name, method="POST", body=json.dumps(payload))

    # -- URL-based virtual media ------------------------------------------
    #
    # The iLO fetches the image itself over HTTP(S); no local file or extra
    # TCP connection is involved, unlike the SCSI-target channel in
    # ilo3console.vmedia. See docs/PROTOCOL_ILO3.md's "URL media" section.

    def vm_status(self) -> Any:
        """Raw ``/json/vm_status``: floppy/USB at ``options[0]``, CD at ``[1]``."""
        return self.get_json("vm_status")

    def insert_url_media(self, device: str, url: str, *, connect: bool = True) -> str:
        """Point the iLO at an HTTP(S) image URL for ``device``.

        ``device`` is ``"FLOPPY"`` or ``"CDROM"``.

        Two requests, matching the reference: INSERT the URL, then (if
        ``connect``) SET boot_option=CONNECT so the iLO actually mounts it.
        Returns ``"Success"`` or the iLO's error string (e.g.
        ``"SCSI_ERR_NO_LICENSE"``).
        """
        if device not in ("FLOPPY", "CDROM"):
            raise ValueError('device must be "FLOPPY" or "CDROM"')
        result = self.post_json(
            "vm_status",
            {
                "method": "set_virtual_media_options",
                "device": device,
                "command": "INSERT",
                "url": url,
                "session_key": self.session_key,
            },
        )
        if connect and result == "Success":
            result = self.post_json(
                "vm_status",
                {
                    "method": "set_virtual_media_options",
                    "device": device,
                    "command": "SET",
                    "boot_option": "CONNECT",
                    "url": url,
                    "session_key": self.session_key,
                },
            )
        return result

    def eject_url_media(self, device: str) -> str:
        """Unmount whatever URL image is attached to ``device``."""
        if device not in ("FLOPPY", "CDROM"):
            raise ValueError('device must be "FLOPPY" or "CDROM"')
        return self.post_json(
            "vm_status",
            {
                "method": "set_virtual_media_options",
                "device": device,
                "command": "EJECT",
                "session_key": self.session_key,
            },
        )

    def rc_info(self) -> RcInfo:
        """Fetch and parse ``/json/rc_info`` into an :class:`RcInfo`."""
        data = self.get_json("rc_info")
        enc_key = data.get("enc_key")
        enc_key_val = bytes.fromhex(enc_key) if enc_key else b""
        rc_port = data.get("rc_port", 23)
        vm_port = data.get("vm_port")
        return RcInfo(
            session_key=self.session_key or "",
            enc_key=enc_key,
            enc_key_val=enc_key_val,
            rc_port=int(rc_port) if rc_port is not None else 23,
            vm_key=data.get("vm_key"),
            vm_port=int(vm_port) if vm_port not in (None, "") else None,
            optional_features=str(data.get("optional_features", "")),
            server_name=data.get("server_name"),
            ilo_fqdn=data.get("ilo_fqdn"),
            raw=data,
        )

    def logout(self) -> None:
        """Best-effort session teardown."""
        if not self.session_key:
            return
        try:
            self.post_json("login_session", {"method": "logout"})
        except (urllib.error.URLError, OSError, ValueError):
            pass
        finally:
            self.session_key = None


class AuthError(Exception):
    """Raised when credential login to the iLO fails."""
