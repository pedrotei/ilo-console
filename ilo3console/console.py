"""The iLO 3 console channel: handshake + encrypted byte pump.

This is the raw TCP channel (default port 23 / ``rc_port``) that carries the
one-way DVC video stream from the iLO and keyboard/mouse/power input to it.
After the firmware turns on encryption (via a DVC command, see
:mod:`ilo3console.dvc`), every byte in both directions is XORed with a keystream
byte. This class owns that keystream state so both the receive pump and
:meth:`send` stay bit-synchronised.

See docs/PROTOCOL_ILO3.md's "Console channel handshake" and "In-protocol
encryption" sections. The DVC decoder lives in a separate module
(:mod:`ilo3console.dvc`) and plugs in as a sink.
"""

from __future__ import annotations

import socket
import struct
from typing import Protocol

from .crypto import CIPHER_NONE, make_keystream

HELLO_BYTE = 0x50  # 'P' — server greeting on both channels.

# requestRemoteConnection command word 0x2001 → little-endian [0x01, 0x20].
_CONSOLE_CMD = 0x2001
CONNECT_SHARE = 0x0056
CONNECT_SEIZE = 0x0055

# Handshake response bytes.
RESP_DENIED = 0x51  # 'Q'
RESP_AUTH = 0x52  # 'R'
RESP_BUSY_A = 0x53  # 'S'
RESP_BUSY_B = 0x59  # 'Y'
RESP_NO_LICENSE = 0x57  # 'W'
RESP_NO_SESSIONS = 0x58  # 'X'


class HandshakeError(Exception):
    """Raised when the console handshake is rejected by the iLO."""


class VideoSink(Protocol):
    """What the console channel feeds decoded (post-decrypt) bytes into.

    The DVC decoder implements this. ``feed`` receives one plaintext byte at a
    time; the decoder calls back into the channel (``set_decrypt_cipher`` /
    ``dvc_encryption``) when it parses an encryption command.
    """

    def feed(self, byte: int) -> None:
        """Consume one decrypted stream byte."""


def xor_session_key(token: str, enc_key_hex: str | None, obfuscate: bool) -> bytes:
    """Obfuscate the session token for the handshake.

    When ``optional_features`` contains ``ENCRYPT_KEY`` the token bytes are XORed
    with the *characters of the enc_key hex string* (not its raw bytes), cycled —
    exactly as ``telnet.requestRemoteConnection`` does.
    """
    raw = bytearray(token.encode("latin-1"))
    if obfuscate and enc_key_hex:
        for i, byte in enumerate(raw):
            raw[i] = byte ^ (ord(enc_key_hex[i % len(enc_key_hex)]) & 0xFF)
    return bytes(raw)


class ConsoleChannel:
    """One authenticated console connection.

    ``enc_key_bytes`` (the raw 16 bytes of ``rc_info.enc_key``) seeds the stream
    ciphers; ``enc_key_hex`` (the original hex string) is used only to obfuscate
    the handshake token. ``optional_features`` gates both.
    """

    def __init__(
        self,
        host: str,
        port: int,
        session_key: str,
        *,
        enc_key_bytes: bytes = b"",
        enc_key_hex: str | None = None,
        optional_features: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.session_key = session_key
        self.enc_key_bytes = enc_key_bytes
        self.enc_key_hex = enc_key_hex
        self.optional_features = optional_features
        self.timeout = timeout

        self._sock: socket.socket | None = None
        self.authenticated = False
        self.licensed = False

        # Encryption state, shared with the decoder.
        self.dvc_encryption = False
        self._decrypt_stream = None
        self._encrypt_stream = None

    # -- connection / handshake ----------------------------------------------

    def connect(self, mode: int = CONNECT_SHARE) -> None:
        """Open the socket and run the handshake.

        ``mode`` selects busy-negotiation behaviour: :data:`CONNECT_SHARE` (join
        an existing session) or :data:`CONNECT_SEIZE` (take it over). Raises
        :class:`HandshakeError` on any non-success response.
        """
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _linger_zero())
        self._sock = sock

        hello = self._read_exact(1)[0]
        if hello != HELLO_BYTE:
            raise HandshakeError(f"expected hello 0x50, got 0x{hello:02x}")

        self._send_raw(self._connect_request(_CONSOLE_CMD))
        self._handle_auth_response(mode)

    def _connect_request(self, command: int) -> bytes:
        lo = command & 0xFF
        hi = (command >> 8) & 0xFF
        obfuscate = "ENCRYPT_KEY" in self.optional_features
        if obfuscate:
            hi |= 0x40 if "ENCRYPT_VMKEY" in self.optional_features else 0x80
        token = xor_session_key(self.session_key, self.enc_key_hex, obfuscate)
        return bytes([lo, hi]) + token

    def _handle_auth_response(self, mode: int) -> None:
        resp = self._read_exact(1)[0]
        if resp == RESP_AUTH:
            self.authenticated = True
            self.licensed = True
            return
        if resp in (RESP_BUSY_A, RESP_BUSY_B):
            self._send_raw(bytes([mode & 0xFF, (mode >> 8) & 0xFF]))
            resp2 = self._read_exact(1)[0]
            if resp2 == RESP_AUTH:
                self.authenticated = True
                self.licensed = True
                return
            raise HandshakeError(f"seize/share refused (0x{resp2:02x})")
        if resp == RESP_DENIED:
            raise HandshakeError("access denied")
        if resp == RESP_NO_LICENSE:
            raise HandshakeError("no remote-console license")
        if resp == RESP_NO_SESSIONS:
            raise HandshakeError("no free console sessions")
        raise HandshakeError(f"unexpected handshake response 0x{resp:02x}")

    # -- encryption control (called by the decoder) --------------------------

    def set_decrypt_cipher(self, cipher: int) -> None:
        """Select the server→client cipher and (de)activate decryption."""
        self._decrypt_stream = make_keystream(cipher, self.enc_key_bytes)
        self._encrypt_stream = make_keystream(cipher, self.enc_key_bytes)
        self.dvc_encryption = cipher != CIPHER_NONE

    # -- send / receive ------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the underlying socket is currently open."""
        return self._sock is not None

    def send(self, frame: bytes) -> None:
        """Send an input frame, XOR-encrypting it if encryption is active.

        A no-op once the channel is closed or the socket has died — a browser
        tab or webview keeps firing input events (e.g. a blur-triggered
        key-release) right up to and after the moment the server drops the
        connection, and that must never crash the caller.
        """
        if self._sock is None:
            return
        if self.dvc_encryption and self._encrypt_stream is not None:
            frame = bytes(
                b ^ self._encrypt_stream.random_value() for b in frame
            )
        try:
            self._sock.sendall(frame)
        except OSError:
            self.close()

    def run_receiver(self, sink: VideoSink, *, should_stop=lambda: False) -> None:
        """Pump received bytes into ``sink`` until the socket closes.

        Each byte is XOR-decrypted (when encryption is active) *before* being fed
        to the sink, matching the reference receive loop. Runs until EOF, a
        socket error, or ``should_stop()`` returns true; always closes the
        channel on the way out so subsequent :meth:`send` calls are safe no-ops.
        """
        assert self._sock is not None
        self._sock.settimeout(1.0)
        try:
            while not should_stop():
                try:
                    chunk = self._sock.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                for byte in chunk:
                    if self.dvc_encryption and self._decrypt_stream is not None:
                        byte ^= self._decrypt_stream.random_value()
                    sink.feed(byte)
        finally:
            self.close()

    def close(self) -> None:
        """Close the socket, if open, and mark the channel disconnected."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- low-level helpers ----------------------------------------------------

    def _send_raw(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def _read_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise HandshakeError("connection closed during handshake")
            buf.extend(chunk)
        return bytes(buf)

    def __enter__(self) -> ConsoleChannel:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _linger_zero() -> bytes:
    return struct.pack("ii", 1, 0)
