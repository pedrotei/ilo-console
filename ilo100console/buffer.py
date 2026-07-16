"""Little-endian read/write cursor over a byte buffer — the Mahogany wire format.

See docs/PROTOCOL_ILO100.md's "Wire framing" section. Unlike iLO 3's
protocol, Mahogany framing has no common outer envelope: each message type
reads/writes its own fixed and variable-length fields directly, so this
class is just a flat cursor, not a message framer.
"""

from __future__ import annotations

import socket
import struct


class LEBuffer:
    """A little-endian byte cursor supporting both read and write.

    Writing appends to an internal ``bytearray``; reading advances an offset
    into whatever ``data`` bytes were supplied (or accumulated by writes).
    """

    def __init__(self, data: bytes = b"") -> None:
        self._buf = bytearray(data)
        self._offset = 0

    # -- cursor management ------------------------------------------------

    @property
    def offset(self) -> int:
        """Current read/write cursor position."""
        return self._offset

    def remaining(self) -> int:
        """Number of unread bytes left after the cursor."""
        return len(self._buf) - self._offset

    def bytes(self) -> bytes:
        """Everything written so far (from the start, not the cursor)."""
        return bytes(self._buf)

    # -- reads --------------------------------------------------------------

    def read_byte(self) -> int:
        """Read one unsigned byte and advance the cursor."""
        b = self._buf[self._offset]
        self._offset += 1
        return b

    def read_bool(self) -> bool:
        """Read one byte as a boolean (nonzero is True)."""
        return self.read_byte() != 0

    def read_bytes(self, n: int) -> bytes:
        """Read the next ``n`` raw bytes and advance the cursor."""
        b = bytes(self._buf[self._offset : self._offset + n])
        self._offset += n
        return b

    def read_string(self, n: int) -> str:
        """Read ``n`` bytes and decode them as latin-1 text."""
        return self.read_bytes(n).decode("latin-1")

    def read_short(self) -> int:
        """Read a little-endian signed 16-bit integer."""
        v = struct.unpack_from("<h", self._buf, self._offset)[0]
        self._offset += 2
        return v

    def read_ushort(self) -> int:
        """Read a little-endian unsigned 16-bit integer."""
        v = struct.unpack_from("<H", self._buf, self._offset)[0]
        self._offset += 2
        return v

    def read_int(self) -> int:
        """Read a little-endian signed 32-bit integer."""
        v = struct.unpack_from("<i", self._buf, self._offset)[0]
        self._offset += 4
        return v

    def read_uint(self) -> int:
        """Read a little-endian unsigned 32-bit integer."""
        v = struct.unpack_from("<I", self._buf, self._offset)[0]
        self._offset += 4
        return v

    # -- writes ---------------------------------------------------------------

    def write_byte(self, v: int) -> None:
        """Append one byte, masked to 8 bits."""
        self._buf.append(v & 0xFF)

    def write_bool(self, v: bool) -> None:
        """Append a boolean as a single 0/1 byte."""
        self._buf.append(1 if v else 0)

    def write_bytes(self, data: bytes) -> None:
        """Append raw bytes as-is."""
        self._buf.extend(data)

    def write_short(self, v: int) -> None:
        """Append a little-endian signed 16-bit integer."""
        self._buf.extend(struct.pack("<h", v))

    def write_int(self, v: int) -> None:
        """Append a little-endian signed 32-bit integer."""
        self._buf.extend(struct.pack("<i", v))


class SocketReader:
    """Blocking exact-length reads from a live socket, little-endian.

    A plain blocking Python socket with no timeout set just blocks until the
    requested bytes arrive (or the connection closes), which is
    behaviorally equivalent to the wire protocol's framing and far simpler
    than a manual polling loop.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def read_bytes(self, n: int) -> bytes:
        """Block until exactly ``n`` bytes are read; raise if the socket
        closes early."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return bytes(buf)

    def read_u8(self) -> int:
        """Read one unsigned byte from the socket."""
        return self.read_bytes(1)[0]

    def read_bool(self) -> bool:
        """Read one byte from the socket as a boolean (nonzero is True)."""
        return self.read_u8() != 0

    def read_i16(self) -> int:
        """Read a little-endian signed 16-bit integer from the socket."""
        return struct.unpack("<h", self.read_bytes(2))[0]

    def read_u16(self) -> int:
        """Read a little-endian unsigned 16-bit integer from the socket."""
        return struct.unpack("<H", self.read_bytes(2))[0]

    def read_i32(self) -> int:
        """Read a little-endian signed 32-bit integer from the socket."""
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_u32(self) -> int:
        """Read a little-endian unsigned 32-bit integer from the socket."""
        return struct.unpack("<I", self.read_bytes(4))[0]
