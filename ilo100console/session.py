"""TCP/SSL connect + auth handshake for the Mahogany KVM channel.

See docs/PROTOCOL_ILO100.md for the full auth flow, including one detail
only live testing revealed: the ``ClientHandshake``'s "password" field is
**not** the account's literal password. It's ``httpdata``, a per-session
token minted by an authenticated HTTP Digest web login (``GET
/kvms.html``) — the browser console receives it as a connection param; a
standalone client has to fetch it itself, which is what
``KvmSession.connect()`` does here before opening the KVM socket. SSL, when
used (``kvmssl``), is just the whole socket wrapped, not an in-band cipher
like iLO 3's DVC.
"""

from __future__ import annotations

import re
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import protocol as p
from .buffer import SocketReader

_HTTPDATA_RE = re.compile(r'NAME="httpdata"\s+VALUE="([0-9A-Fa-f]+)"')

# This BMC's embedded httpd is old and easy to overload: it can reset a
# connection mid-response (raw ConnectionResetError, not even wrapped as a
# clean HTTPError/URLError -- confirmed live, see docs/PROTOCOL_ILO100.md)
# and recover anywhere from under a second to tens of seconds later. This
# fetch is read-only (safe to retry) and only runs on a deliberate, occasional
# user action (clicking "connect"), so it's worth a generous budget rather
# than giving up in ~2s and leaving the user staring at a raw errno.
_TRANSIENT_RETRY_DELAYS = (0.5, 1, 2, 3, 5, 5)


class HandshakeError(Exception):
    """Raised when the Mahogany handshake is rejected or malformed."""


class _TransientConnectionError(Exception):
    """Internal: a raw reset worth retrying, as opposed to a real auth/URL
    failure (wrong password, unreachable host) which should fail fast."""


def _fetch_kvms_html(
    url: str, opener: urllib.request.OpenerDirector, timeout: float
) -> str:
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.read().decode("latin-1")
    except urllib.error.HTTPError as exc:
        raise HandshakeError(f"web login to {url} failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HandshakeError(f"cannot reach {url}: {exc.reason}") from exc
    except ConnectionError as exc:
        raise _TransientConnectionError(str(exc)) from exc


def fetch_httpdata_token(
    host: str,
    username: str,
    password: str,
    *,
    web_port: int | None = None,
    use_ssl: bool = False,
    timeout: float = 15.0,
) -> str:
    """HTTP-Digest-authenticate and pull the per-session ``httpdata`` token
    out of ``kvms.html``, exactly as the iLO's own web console receives it
    as a connection param. Raises :class:`HandshakeError` on any
    login/parse failure.
    """
    port = web_port if web_port is not None else (443 if use_ssl else 80)
    scheme = "https" if use_ssl else "http"
    default_port = 443 if use_ssl else 80
    suffix = "" if port == default_port else f":{port}"
    url = f"{scheme}://{host}{suffix}/kvms.html"

    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, url, username, password)
    handlers = [urllib.request.HTTPDigestAuthHandler(pwd_mgr)]
    if use_ssl:
        # Matches the KVM socket's own trust-everything policy
        # (ServerEgninesTrustManager) — self-signed embedded-device certs.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    html: str | None = None
    last_exc: _TransientConnectionError | None = None
    for delay in (0.0,) + _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            html = _fetch_kvms_html(url, opener, timeout)
            break
        except _TransientConnectionError as exc:
            last_exc = exc
    if html is None:
        raise HandshakeError(
            f"connection to {url} was reset repeatedly: {last_exc}"
        ) from last_exc

    match = _HTTPDATA_RE.search(html)
    if not match:
        raise HandshakeError(
            f"no httpdata token found in {url} response (wrong credentials, "
            "or Virtual KVM/Media isn't licensed/enabled for this account?)"
        )
    return match.group(1)


class KvmSession:
    """One connected-and-handshaked KVM socket."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout

        self._sock: socket.socket | None = None
        self.reader: SocketReader | None = None
        self.handshake: p.ServerHandshake | None = None

    @property
    def connected(self) -> bool:
        """Whether the KVM socket is currently open."""
        return self._sock is not None

    def connect(self) -> p.ServerHandshake:
        """Fetch the httpdata token, open the KVM socket (SSL-wrapped if requested),
        and perform the ``ClientHandshake``/``ServerHandshake`` exchange."""
        # self.password is the account's real login password; the KVM
        # handshake itself needs the per-session httpdata token instead (see
        # module docstring) — fetched via the same web login a browser does.
        token = fetch_httpdata_token(
            self.host,
            self.username,
            self.password,
            web_port=self.port,
            use_ssl=self.use_ssl,
            timeout=self.timeout,
        )

        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            if self.use_ssl:
                # The reference trusts any certificate
                # (ServerEgninesTrustManager); these are self-signed
                # embedded-device certs with no real CA chain.
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(raw, server_hostname=self.host)
            else:
                sock = raw
        except Exception:
            raw.close()
            raise
        self._sock = sock
        self.reader = SocketReader(sock)

        sock.sendall(p.encode_client_nop())
        # Request every privilege, matching a live capture of the iLO's own
        # web console byte-for-byte (it always requests everything
        # regardless of what it actually uses); the server still only
        # enables what the account/license permits, per ServerHandshake.
        sock.sendall(
            p.encode_client_handshake(self.username, token, p.CFG_EVERYTHING)
        )

        msg_id = self.reader.read_u8()
        if msg_id != p.MSG_SERVER_HANDSHAKE:
            self.close()
            raise HandshakeError(f"expected ServerHandshake (200), got {msg_id}")
        handshake = p.decode_server_handshake(self.reader)
        self.handshake = handshake
        return handshake

    def send(self, frame: bytes) -> None:
        """Send a raw encoded frame; close the session on any socket error."""
        if self._sock is None:
            return
        try:
            self._sock.sendall(frame)
        except OSError:
            self.close()

    def close(self) -> None:
        """Close the socket, if open, and clear the reader/socket state."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self.reader = None
