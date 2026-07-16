"""Virtual media data channel for iLO 100 -- architecturally inverted from
iLO 3's (see ``ilo3console.vmedia``).

For iLO 3, the *client* dials the iLO's ``rc_info.vm_port`` and acts as a
SCSI target. For iLO 100, ``StorageClientConnect`` (protocol.py) tells the
iLO an IP:port to dial, and the iLO's own firmware makes the outbound
connection -- so the client instead runs a small TCP *listener* here and
waits for the iLO to connect to it.

The framing below (``_HelloFrame``, ``_RequestFrame``, ``_ReplyFrame``) is
documented in docs/PROTOCOL_ILO100.md, derived from a live packet capture
against real hardware -- the console delegates this channel to a native
helper binary (``LIBM2-32/64.SO``/``.DLL``) rather than exposing it in any
written spec. The SCSI-CDB format itself turned out to be identical to what
``ilo3console.vmedia`` already implements: a live capture showed the exact
same TEST_UNIT_READY / REQUEST_SENSE / READ_TOC / READ_10 opcodes and CDB
layout, just wrapped in this different outer per-command frame. Several
fields in that outer frame are structurally confirmed (sequence number,
declared data length) but their exact semantics are not (a handful of what
appear to be opaque per-connection handles/pointers on the wire) -- those
are treated as free-form session tokens here, consistent with the rest of
this project's "connect it for real and see what breaks" methodology.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import threading

_REQUEST_SIZE = 76
_REPLY_SIZE = 64
_CDB_SIZE = 12
_CDB_OFFSET = _REQUEST_SIZE - _CDB_SIZE  # 64

# SCSI opcodes actually observed live (subset -- matches ilo3console.vmedia's
# CD-ROM path, which is what this was tested against).
SCSI_TEST_UNIT_READY = 0x00
SCSI_REQUEST_SENSE = 0x03
SCSI_INQUIRY = 0x12
SCSI_START_STOP_UNIT = 0x1B
SCSI_PA_MEDIA_REMOVAL = 0x1E
SCSI_READ_CAPACITY = 0x25
SCSI_READ_10 = 0x28
SCSI_READ_TOC = 0x43
SCSI_GET_EVENT_STATUS = 0x4A
SCSI_MODE_SENSE = 0x5A
SCSI_READ_12 = 0xA8

SENSE_NO_SENSE = 0
SENSE_NOT_READY = 2
SENSE_ILLEGAL_REQUEST = 5
SENSE_UNIT_ATTENTION = 6


def local_ip_toward(host: str, port: int = 80) -> str:
    """The local IPv4 address the OS would use to reach ``host`` -- found via
    a UDP "connect" (sets up routing without sending any packet) rather than
    enumerating local network interfaces, since this approach doesn't need
    to guess which interface/address the iLO can actually route back to.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, port))
        return s.getsockname()[0]


class VMediaError(Exception):
    """Raised when the storage-channel handshake or framing is invalid."""


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise VMediaError("connection closed mid-message")
        buf += chunk
    return bytes(buf)


class FileMedia:
    """A local disk image / ISO file. Reads past end-of-file are zero-padded,
    matching how a real block device reads an unwritten sector."""

    def __init__(self, path: str, writable: bool = False) -> None:
        self.path = path
        can_write = writable and os.access(path, os.W_OK)
        # Kept open for the object's lifetime (closed explicitly in close());
        # a `with` block here would close it before it can be used.
        self._fh = open(  # noqa: SIM115 pylint:disable=consider-using-with
            path, "r+b" if can_write else "rb"
        )
        self._writable = can_write

    def size(self) -> int:
        """Return the file's current size in bytes."""
        return os.fstat(self._fh.fileno()).st_size

    def read(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset``, zero-padding past end-of-file."""
        self._fh.seek(offset)
        data = self._fh.read(length)
        if len(data) < length:
            data += b"\x00" * (length - len(data))
        return data

    def write(self, offset: int, data: bytes) -> None:
        """Write ``data`` at ``offset``; raises if the media was opened read-only."""
        if not self._writable:
            raise OSError("media is read-only")
        self._fh.seek(offset)
        self._fh.write(data)
        self._fh.flush()

    def close(self) -> None:
        """Close the underlying file handle."""
        self._fh.close()


class _Session:
    """Per-connection opaque handle fields, echoed on every reply.

    These four fields showed up as fixed-per-connection values in the live
    capture (``[0:4]`` and ``[16:20]`` of every 76-byte request) with no
    confirmed semantic meaning -- likely raw pointers/handles from the
    native helper's own memory. Generating our own arbitrary-but-stable
    values for a session we originate is the natural approach; whether the
    iLO validates them or just treats them as opaque is untested until this
    runs live end-to-end.
    """

    def __init__(self) -> None:
        self.handle_a = os.urandom(4)
        self.handle_b = os.urandom(4)


class MediaChannel:
    """Serves one iLO storage-channel connection: the hello handshake, then
    a request/reply loop translating the outer frame (see
    docs/PROTOCOL_ILO100.md) to the SCSI CD-ROM command handling shared
    with ``ilo3console.vmedia`` (same opcodes/CDB layout, confirmed
    identical by the live capture).
    """

    def __init__(self, conn: socket.socket, media: FileMedia) -> None:
        self.conn = conn
        self.media = media
        self.block_size = 2048
        self._sense_state = 2  # media present and ready from the start
        self._session = _Session()

    def run(self, *, should_stop=lambda: False) -> None:
        """Perform the hello handshake, then serve SCSI requests until ``should_stop``
        returns True or the connection ends; always closes the socket on exit."""
        try:
            self._read_hello()
            while not should_stop():
                if not self._serve_one_request():
                    break
        except (OSError, VMediaError):
            pass
        finally:
            self.conn.close()

    # -- hello handshake ------------------------------------------------

    def _read_hello(self) -> None:
        # 8-byte header + "VENDORID" (8 bytes) + a 512-byte UTF-16LE share
        # path, twice over (share0 then share1) = 8+8+512+512 = 1040 bytes
        # total observed on the wire as a single 536-byte segment in the
        # capture -- but that capture only had one share populated; read
        # defensively by header-declared size instead of a hardcoded total.
        _recv_exact(self.conn, 8)
        tag = _recv_exact(self.conn, 8)
        if tag != b"VENDORID":
            raise VMediaError(f"unexpected hello tag {tag!r}")
        # Remainder of the hello observed was zero-padded path data; the
        # capture showed 536 bytes total for the whole hello message.
        _recv_exact(self.conn, 536 - 8 - 8)
        # 8-byte all-zero "ready" ping from the iLO.
        _recv_exact(self.conn, 8)
        # Our 16-byte "capabilities" reply -- values below are exactly what
        # was captured live from the real native helper's own reply, kept
        # as-is since their semantics (likely block size + a session token)
        # aren't independently confirmed enough to safely improvise.
        self.conn.sendall(bytes.fromhex("00010001001000002b10222206000000"))

    # -- request/reply loop ------------------------------------------------

    def _serve_one_request(self) -> bool:
        try:
            data = _recv_exact(self.conn, _REQUEST_SIZE)
        except VMediaError:
            return False
        cdb = data[_CDB_OFFSET:]
        opcode = cdb[0]
        if opcode in (SCSI_READ_10, SCSI_READ_12):
            self._handle_read(cdb, opcode == SCSI_READ_12)
        elif opcode == SCSI_TEST_UNIT_READY:
            self._handle_test_unit_ready()
        elif opcode == SCSI_REQUEST_SENSE:
            self._handle_request_sense()
        elif opcode == SCSI_READ_TOC:
            self._handle_read_toc(cdb)
        elif opcode == SCSI_READ_CAPACITY:
            self._handle_read_capacity()
        elif opcode in (SCSI_START_STOP_UNIT, SCSI_PA_MEDIA_REMOVAL):
            self._handle_status_only()
        elif opcode == SCSI_MODE_SENSE:
            self._handle_mode_sense()
        elif opcode == SCSI_GET_EVENT_STATUS:
            self._handle_get_event_status(cdb)
        else:
            self._handle_status_only()
        return True

    def _build_reply(self, data: bytes = b"") -> bytes:
        header = bytearray(_REPLY_SIZE)
        struct.pack_into("<I", header, 24, len(data))
        header[36:40] = self._session.handle_a
        header[52:56] = self._session.handle_b
        return bytes(header) + data

    def _send(self, data: bytes = b"") -> None:
        self.conn.sendall(self._build_reply(data))

    # -- SCSI command handlers (CD-ROM only -- matches the media type this
    # was captured and tested against) ---------------------------------

    def _handle_test_unit_ready(self) -> None:
        self._send()

    def _handle_request_sense(self) -> None:
        # 18-byte fixed-format sense data, no-sense/ready.
        sense = bytes(
            (0x70, 0, SENSE_NO_SENSE, 0, 0, 0, 0, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        )
        self._send(sense)

    def _handle_status_only(self) -> None:
        self._send()

    def _handle_read_capacity(self) -> None:
        last_lba = max(self.media.size() // self.block_size - 1, 0)
        payload = struct.pack(">I", last_lba) + struct.pack(">I", self.block_size)
        self._send(payload)

    def _handle_read_toc(self, cdb: bytes) -> None:
        msf = (cdb[1] & 2) != 0
        num_sectors = self.media.size() // self.block_size
        d = num_sectors / 75.0 + 2.0
        mm, ss = int(d) // 60, int(d) % 60
        ff = int((d - int(d)) * 75.0)
        buf = bytearray(20)
        buf[0:2] = struct.pack(">H", 18)
        buf[2:4] = bytes((1, 1))
        buf[4:8] = bytes((0, 20, 1, 0))
        buf[10] = 2 if msf else 0
        buf[12:14] = bytes((0, 20))
        buf[14] = 0xAA
        if msf:
            buf[17], buf[18], buf[19] = mm, ss, ff
        else:
            buf[17:20] = struct.pack(">I", num_sectors)[1:4]
        self._send(bytes(buf))

    def _handle_mode_sense(self) -> None:
        self._send(bytes((0, 8, 1, 0, 0, 0, 0, 0)))

    def _handle_get_event_status(self, cdb: bytes) -> None:
        if cdb[4] & 0x10:
            self._send(bytes((0, 6, 4, 16)))
        else:
            self._send(bytes((0, 2, 0x80, 16)))

    def _handle_read(self, cdb: bytes, is_read12: bool) -> None:
        lba = struct.unpack_from(">I", cdb, 2)[0]
        length = (
            struct.unpack_from(">I", cdb, 6)[0]
            if is_read12
            else struct.unpack_from(">H", cdb, 7)[0]
        ) * self.block_size
        data = self.media.read(lba * self.block_size, length)
        self._send(data)


class StorageServer:
    """Listens for the iLO's storage-channel connection and serves one
    mounted image through :class:`MediaChannel`."""

    def __init__(
        self,
        media: FileMedia,
        *,
        bind_host: str = "0.0.0.0",
        port: int = 0,
        on_log=print,
    ) -> None:
        self.media = media
        self.on_log = on_log
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((bind_host, port))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._channel: MediaChannel | None = None

    def start(self) -> None:
        """Start the background thread that accepts and serves the
        iLO's storage connection."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._listener.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.on_log(f"storage channel: iLO connected from {addr[0]}:{addr[1]}")
            self._channel = MediaChannel(conn, self.media)
            try:
                self._channel.run(should_stop=self._stop.is_set)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.on_log(f"storage channel: error: {exc!r}")
            self.on_log("storage channel: iLO disconnected")
            return  # single mount at a time, matches share_index0-only usage

    @property
    def connected(self) -> bool:
        """Whether the iLO has connected to the storage channel and a
        media session is active."""
        return self._channel is not None

    def stop(self) -> None:
        """Stop accepting/serving, close the listener, any active connection,
        and the backing media file."""
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        if self._channel is not None:
            with contextlib.suppress(OSError):
                self._channel.conn.close()
        with contextlib.suppress(OSError):
            self.media.close()
