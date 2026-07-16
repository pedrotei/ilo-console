"""The iLO 3 command channel — out-of-band status notifications.

A second TCP connection to the same host/port as the console, distinguished by
sending command byte ``0x02`` (vs. ``0x01`` for the console) in its handshake.
It carries power state, health LED, POST-complete, and the seize / share /
firmware-upgrade / unlicensed events. See docs/PROTOCOL_ILO3.md's "Command
channel" section.

Each message is a fixed 12-byte header — ``[0]=type``, ``[4]=payload size``,
``[8:10]`` and ``[10:12]`` little-endian fields — followed by ``size`` payload
bytes for the types that carry one.
"""

from __future__ import annotations

import socket
import struct

from .console import HELLO_BYTE, RESP_AUTH, HandshakeError, xor_session_key

# Command-channel handshake command word 0x2002 → [0x02, 0x20].
_CMD_CMD = 0x2002


class CmdController:
    """Sink for command-channel events. Override what you need."""

    def on_post_complete(self) -> None:
        """Called when the server finishes POST."""

    def on_power(self, on: bool) -> None:
        """Called when the server's power state changes."""

    def on_health(self, level: int) -> None:
        """Called when the server's health LED level changes."""

    def on_progress_code(self, code: int) -> None:
        """Called with a POST progress code update."""

    def on_seize_request(self, user: str, host: str) -> bool:  # pylint: disable=unused-argument
        """Return True to *allow* the seize (default: deny)."""
        return False

    def on_shared(self, user: str, host: str) -> None:
        """Called when another user shares the console session."""

    def on_vm_ack(self, status: int, device: int, connected: int) -> None:
        """Called with the result of a virtual-media mount/unmount request."""

    def on_firmware_upgrade(self) -> None:
        """Called when the iLO reports a firmware upgrade is in progress."""

    def on_unauthorized(self, target: str) -> None:
        """Called when access to ``target`` (console/vmedia/power) is denied."""

    def on_unlicensed(self) -> None:
        """Called when the session lacks the required license."""

    def on_reset(self) -> None:
        """Called when the iLO signals a reset."""


class CmdChannel:
    """One command-channel connection with a blocking receive loop."""

    def __init__(
        self,
        host: str,
        port: int,
        session_key: str,
        controller: CmdController,
        *,
        enc_key_hex: str | None = None,
        optional_features: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.session_key = session_key
        self.ctl = controller
        self.enc_key_hex = enc_key_hex
        self.optional_features = optional_features
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        """Open the TCP connection and run the command-channel handshake.

        Raises :class:`HandshakeError` on a bad hello byte or failed auth.
        """
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        if self._recv_exact(1)[0] != HELLO_BYTE:
            raise HandshakeError("command channel: bad hello byte")

        lo = _CMD_CMD & 0xFF
        hi = (_CMD_CMD >> 8) & 0xFF
        obfuscate = "ENCRYPT_KEY" in self.optional_features
        if obfuscate:
            hi |= 0x40 if "ENCRYPT_VMKEY" in self.optional_features else 0x80
        token = xor_session_key(self.session_key, self.enc_key_hex, obfuscate)
        self._sock.sendall(bytes([lo, hi]) + token)

        if self._recv_exact(1)[0] != RESP_AUTH:
            raise HandshakeError("command channel: authentication failed")

    def run(self, *, should_stop=lambda: False) -> None:
        """Read and dispatch messages until the socket closes."""
        assert self._sock is not None
        self._sock.settimeout(1.0)
        while not should_stop():
            try:
                header = self._recv_exact(12)
            except TimeoutError:
                continue
            except (HandshakeError, OSError):
                break
            if header is None:
                break
            self._dispatch(header)

    def _dispatch(self, header: bytes) -> None:
        msg_type = header[0]
        field10 = struct.unpack_from("<h", header, 10)[0]

        if msg_type == 2:
            self.ctl.on_post_complete()
        elif msg_type == 3:
            self.ctl.on_power(self._recv_exact(1)[0] != 0)
        elif msg_type == 4:
            self.ctl.on_health(self._recv_exact(1)[0])
        elif msg_type == 5:
            payload = self._recv_exact(2)
            self.ctl.on_progress_code(payload[1] << 8 | payload[0])
        elif msg_type == 6:
            user, host = self._read_two_strings()
            allow = self.ctl.on_seize_request(user, host)
            self._send_bool(allow)
        elif msg_type == 7:
            p = self._recv_exact(4)
            self.ctl.on_vm_ack(p[0], p[1], p[3])
        elif msg_type == 9:
            # No reply: only type 6 (seize request) gets a boolean reply
            # (see docs/PROTOCOL_ILO3.md's command-channel message table) --
            # replying here desyncs every subsequent header read.
            user, host = self._read_two_strings()
            self.ctl.on_shared(user, host)
        elif msg_type == 10:
            self.ctl.on_firmware_upgrade()
        elif msg_type == 11:
            target = {2: "remote console", 3: "virtual media", 4: "power"}.get(
                field10, f"0x{field10:x}"
            )
            self.ctl.on_unauthorized(target)
        elif msg_type == 13:
            self._recv_exact(1)
        elif msg_type == 14:
            self.ctl.on_unlicensed()
        elif msg_type == 15:
            self.ctl.on_reset()

    def _read_two_strings(self):
        payload = self._recv_exact(128)
        user = payload[0:64].split(b"\x00", 1)[0].decode("latin-1").strip()
        host = payload[64:128].split(b"\x00", 1)[0].decode("latin-1").strip()
        return (user or "UNKNOWN", host or "UNKNOWN")

    def _send_bool(self, value: bool) -> None:
        assert self._sock is not None
        self._sock.sendall(bytes([4 if value else 3, 0, 0, 0]))

    def close(self) -> None:
        """Close the socket, if open."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise HandshakeError("command channel closed")
            buf.extend(chunk)
        return bytes(buf)
