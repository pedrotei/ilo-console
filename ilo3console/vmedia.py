"""Virtual media channel — mount a local disk image or ISO on the iLO.

A second TCP connection to ``rc_info.vm_port`` (default 17988), separate from
the console/command channels. Direction is inverted from those: here **we act
as a SCSI target** and the iLO acts as the initiator, issuing SCSI CDBs to read
(and, for floppy/USB media, write) blocks of whatever we're serving — a local
disk image file. The iLO's firmware then presents that as an attached
USB/SCSI device to the physical server.

See docs/PROTOCOL_ILO3.md's "Local-file media" section. Two things worth
noting:

- The handshake computes an MD5 digest of the ``INFO0``/session-key
  material in the firmware's own reference behaviour, but that digest is
  never actually placed on the wire — the handshake sends the raw session
  key, which is what's implemented here.
- The same device class is used for *both* floppy- and USB-key-shaped
  media; which one is announced in the handshake is decided purely by image
  size (over ~2.88MB is announced as a USB key). CD/DVD media is always
  read-only.

Only file-backed media is supported (no raw local device passthrough — there's
no equivalent of a physical floppy/USB drive to hand off in a headless Python
client). URL-based virtual media (the iLO fetching an ISO/IMG itself) doesn't
use this channel at all — see ``session.vm_status`` / ``set_virtual_media``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import threading

from .console import xor_session_key

HELLO_BYTE = 0x50  # unused here (VM channel has no hello byte) — see below.

# -- handshake --------------------------------------------------------------

_HELLO_CMD = 0x10
_HELLO_OK = 0x20

DEV_FLOPPY = 1
DEV_CDROM = 2
DEV_USBKEY = 3

# Floppy images larger than this are announced as a USB key instead
# (0x2D0000 = 2880 KiB, the largest real floppy size).
FLOPPY_USBKEY_THRESHOLD = 0x2D0000

# -- SCSI opcodes (subset actually handled, see docs/PROTOCOL_ILO3.md) -------

SCSI_TEST_UNIT_READY = 0x00
SCSI_FORMAT_UNIT = 0x04
SCSI_SEND_DIAGNOSTIC = 0x1D
SCSI_START_STOP_UNIT = 0x1B
SCSI_PA_MEDIA_REMOVAL = 0x1E
SCSI_READ_CAPACITIES = 0x23
SCSI_READ_CAPACITY = 0x25
SCSI_READ_10 = 0x28
SCSI_WRITE_10 = 0x2A
SCSI_WRITE_VERIFY = 0x2E
SCSI_READ_TOC = 0x43
SCSI_GET_EVENT_STATUS = 0x4A
SCSI_MODE_SENSE = 0x5A
SCSI_READ_12 = 0xA8
SCSI_WRITE_12 = 0xAA

SYNC_ECHO_CMD = 0xFE  # a 12-byte ping the iLO can send; echo bytes [4:12] back.

# -- SCSI sense keys (subset used in replies) --------------------------------

SENSE_NO_SENSE = 0
SENSE_NOT_READY = 2
SENSE_MEDIUM_ERROR = 3
SENSE_ILLEGAL_REQUEST = 5
SENSE_UNIT_ATTENTION = 6
SENSE_DATA_PROTECT = 7


class VMediaError(Exception):
    """Raised when the virtual-media handshake is rejected by the iLO."""


class ReplyHeader:
    """The 16-byte header that precedes every server->initiator reply.

    Layout: 4-byte magic, little-endian int32 flags, media byte, sense_key,
    asc, ascq, little-endian int32 payload length. See docs/PROTOCOL_ILO3.md's
    "Local-file media" section.
    """

    MAGIC = bytes((0xDE, 0xC0, 0xAD, 0x0B))
    FLAG_WP = 1
    FLAG_KEEPALIVE = 2
    FLAG_DISCONNECT = 4

    def __init__(self) -> None:
        self.flags = 0
        self.media = 0
        self.sense_key = 0
        self.asc = 0
        self.ascq = 0
        self.length = 0

    def set(self, sense_key: int, asc: int, ascq: int, length: int) -> None:
        """Set the sense key/ASC/ASCQ and payload length for the next reply."""
        self.sense_key = sense_key & 0xFF
        self.asc = asc & 0xFF
        self.ascq = ascq & 0xFF
        self.length = length

    def set_media(self, media: int) -> None:
        """Set the media-type byte reported in the reply header."""
        self.media = media & 0xFF

    def set_flag(self, bit: int, on: bool) -> None:
        """Set or clear a single flag bit (e.g. write-protect, keepalive)."""
        self.flags = (self.flags | bit) if on else (self.flags & ~bit)

    def to_bytes(self) -> bytes:
        """Serialize the header to its 16-byte wire representation."""
        return (
            self.MAGIC
            + struct.pack("<i", self.flags)
            + bytes((self.media, self.sense_key, self.asc, self.ascq))
            + struct.pack("<i", self.length)
        )

    def to_bytes_synch(self, req: bytes) -> bytes:
        """Reply to a :data:`SYNC_ECHO_CMD` ping: echo ``req[4:12]`` verbatim."""
        return self.MAGIC + struct.pack("<i", self.flags) + req[4:12]


class FileMedia:
    """A local disk image / ISO file, opened for the virtual-media channel.

    Reads past end-of-file are zero-padded rather than raised, matching how a
    real block device reads an unwritten sector. Opened read-only unless
    ``writable`` is explicitly requested and the file permits it.
    """

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
        """Return the current size in bytes of the backing file."""
        return os.fstat(self._fh.fileno()).st_size

    def read(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset``, zero-padding any read past EOF."""
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

    @property
    def writable(self) -> bool:
        """Whether the backing file was opened for writing."""
        return self._writable

    def close(self) -> None:
        """Close the underlying file handle."""
        self._fh.close()

    def __enter__(self) -> FileMedia:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def pick_device_kind(kind: str, size: int) -> int:
    """Resolve a requested media kind + image size to a handshake device id."""
    if kind == "cdrom":
        return DEV_CDROM
    if kind == "usbkey":
        return DEV_USBKEY
    if kind == "floppy":
        return DEV_FLOPPY
    if kind == "auto":
        return DEV_USBKEY if size > FLOPPY_USBKEY_THRESHOLD else DEV_FLOPPY
    raise ValueError(f"unknown media kind: {kind!r}")


class VirtualMediaChannel:
    """One mounted virtual-media connection: handshake + SCSI-target loop.

    ``kind`` is ``"cdrom"``, ``"floppy"``, ``"usbkey"``, or ``"auto"`` (floppy
    vs. USB key chosen by image size — see docs/PROTOCOL_ILO3.md).
    """

    def __init__(
        self,
        host: str,
        port: int,
        session_key: str,
        media: FileMedia,
        *,
        kind: str = "auto",
        enc_key_hex: str | None = None,
        optional_features: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.session_key = session_key
        self.media = media
        self.device = pick_device_kind(kind, media.size())
        self.is_cdrom = self.device == DEV_CDROM
        self.block_size = 2048 if self.is_cdrom else 512
        self.enc_key_hex = enc_key_hex
        self.optional_features = optional_features
        self.timeout = timeout

        self._sock: socket.socket | None = None
        self._sense_state = 0  # 0=not-ready 1=changed(first poll) 2=ready
        self._reply = ReplyHeader()

    @property
    def writable(self) -> bool:
        """Whether writes are permitted (never for CD-ROM media)."""
        return not self.is_cdrom and self.media.writable

    @property
    def connected(self) -> bool:
        """Whether the handshake socket is currently open."""
        return self._sock is not None

    # -- handshake --------------------------------------------------------------

    def connect(self) -> None:
        """Open the TCP connection and perform the virtual-media handshake.

        Sends the device id and (optionally obfuscated) session key, then
        raises :class:`VMediaError` if the iLO rejects it.
        """
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

        obfuscate = "ENCRYPT_VMKEY" in self.optional_features
        token = xor_session_key(self.session_key, self.enc_key_hex, obfuscate)
        # [cmd=0x10, device | 0x80 (we never present a real local device), key(32)]
        frame = bytes((_HELLO_CMD, self.device | 0x80)) + token
        sock.sendall(frame)

        resp = self._recv_handshake_reply()
        if resp[0] != _HELLO_OK or resp[1] != 0:
            self.close()
            raise VMediaError(f"virtual media handshake rejected (0x{resp[0]:02x})")

    def _recv_handshake_reply(self) -> bytes:
        buf = bytearray()
        while len(buf) < 4:
            chunk = self._sock.recv(4 - len(buf))
            if not chunk:
                raise VMediaError("connection closed during handshake")
            buf += chunk
        return bytes(buf)

    # -- SCSI-target loop -------------------------------------------------------

    def run(self, *, should_stop=lambda: False) -> None:
        """Serve SCSI requests until the iLO disconnects or ``should_stop()``."""
        assert self._sock is not None
        try:
            while not should_stop():
                req = self._read_command()
                if req is None:
                    break
                if self.is_cdrom:
                    self._process_cdrom(req)
                else:
                    self._process_floppy(req)
        except (OSError, VMediaError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        """Send a disconnect flag to the iLO (best-effort) and close the socket."""
        if self._sock is not None:
            try:
                if self.media is not None:
                    self._reply.set_flag(ReplyHeader.FLAG_DISCONNECT, True)
                    with contextlib.suppress(OSError):
                        self._sock.sendall(self._reply.to_bytes())
                self._sock.close()
            finally:
                self._sock = None

    # -- wire helpers -------------------------------------------------------

    def _read_command(self) -> bytes | None:
        """Read a 12-byte SCSI CDB, sending keepalives while idle.

        Returns ``None`` on a clean disconnect. A :data:`SYNC_ECHO_CMD` ping is
        answered inline and does not count as a command.
        """
        buf = bytearray()
        while len(buf) < 12:
            self._sock.settimeout(1.0)
            try:
                chunk = self._sock.recv(12 - len(buf))
            except TimeoutError:
                if not buf:
                    self._reply.set_flag(ReplyHeader.FLAG_KEEPALIVE, True)
                    self._sock.sendall(self._reply.to_bytes())
                    self._reply.set_flag(ReplyHeader.FLAG_KEEPALIVE, False)
                continue
            if not chunk:
                return None
            buf += chunk
        req = bytes(buf)
        if req[0] == SYNC_ECHO_CMD:
            self._sock.sendall(self._reply.to_bytes_synch(req))
            return self._read_command()
        return req

    def _read_complete(self, length: int) -> bytes:
        buf = bytearray()
        while len(buf) < length:
            self._sock.settimeout(1.0)
            try:
                chunk = self._sock.recv(length - len(buf))
            except TimeoutError:
                continue
            if not chunk:
                raise VMediaError("connection closed mid-payload")
            buf += chunk
        return bytes(buf)

    def _send(self, payload: bytes = b"") -> None:
        self._sock.sendall(self._reply.to_bytes())
        if payload:
            self._sock.sendall(payload)

    def _bump_sense_state(self) -> None:
        size = self.media.size()
        if size <= 0:
            self._sense_state = 0
        else:
            self._sense_state = min(self._sense_state + 1, 2)

    @staticmethod
    def _u16(req: bytes, off: int) -> int:
        return (req[off] << 8) | req[off + 1]

    @staticmethod
    def _u24(req: bytes, off: int) -> int:
        return (req[off] << 16) | (req[off + 1] << 8) | req[off + 2]

    @staticmethod
    def _u32(req: bytes, off: int) -> int:
        return (
            (req[off] << 24)
            | (req[off + 1] << 16)
            | (req[off + 2] << 8)
            | req[off + 3]
        )

    # -- CD/DVD image (SCSIcdimage) ------------------------------------------

    def _process_cdrom(self, req: bytes) -> None:
        op = req[0]
        self._bump_sense_state()
        self._reply.set_media(1 if self.media.size() else 0)

        if op == SCSI_PA_MEDIA_REMOVAL:
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
            self._send()
        elif op == SCSI_READ_CAPACITY:
            self._cdrom_read_capacity()
        elif op == SCSI_SEND_DIAGNOSTIC:
            pass  # no-op, matches the reference (empty method body)
        elif op == SCSI_TEST_UNIT_READY:
            self._cdrom_test_unit_ready()
        elif op in (SCSI_READ_10, SCSI_READ_12):
            self._cdrom_read(req)
        elif op == SCSI_START_STOP_UNIT:
            self._cdrom_start_stop_unit(req)
        elif op == SCSI_READ_TOC:
            self._cdrom_read_toc(req)
        elif op == SCSI_MODE_SENSE:
            self._cdrom_mode_sense()
        elif op == SCSI_GET_EVENT_STATUS:
            self._cdrom_get_event_status(req)
        else:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x24, 0, 0)
            self._send()

    def _cdrom_test_unit_ready(self) -> None:
        if self._sense_state == 0:
            self._reply.set(SENSE_NOT_READY, 0x3A, 0, 0)
        elif self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, 0)
            self._sense_state = 2
        else:
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        self._send()

    def _cdrom_read_capacity(self) -> None:
        payload = bytearray(8)
        if self._sense_state == 0:
            self._reply.set(SENSE_NOT_READY, 0x3A, 0, 0)
        elif self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, 0)
        else:
            last_lba = self.media.size() // 2048 - 1
            payload[0:4] = struct.pack(">i", last_lba)
            payload[6] = 8
            self._reply.set(SENSE_NO_SENSE, 0, 0, len(payload))
        self._send(bytes(payload) if self._sense_state == 2 else b"")

    def _cdrom_read(self, req: bytes) -> None:
        is_read12 = req[0] == 0xA8
        lba = self._u32(req, 2) * 2048
        length = (self._u32(req, 6) if is_read12 else self._u16(req, 7)) * 2048
        size = self.media.size()
        if self._sense_state == 0:
            self._reply.set(SENSE_NOT_READY, 0x3A, 0, 0)
            length = 0
        elif self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, 0)
            length = 0
            self._sense_state = 2
        elif 0 <= lba < size:
            data = self.media.read(lba, length)
            self._reply.set(SENSE_NO_SENSE, 0, 0, length)
            self._send(data)
            return
        else:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x21, 0, 0)
            length = 0
        self._send()

    def _cdrom_start_stop_unit(self, req: bytes) -> bool:
        self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        self._send()
        if (req[4] & 3) == 2:
            self._sense_state = 0
            return False
        return True

    def _cdrom_read_toc(self, req: bytes) -> None:
        msf = (req[1] & 2) != 0
        fmt = (req[9] & 0xC0) >> 6
        num_sectors = self.media.size() // 2048
        d = num_sectors / 75.0 + 2.0
        mm, ss = int(d) // 60, int(d) % 60
        ff = int((d - int(d)) * 75.0)
        alloc = self._u16(req, 7)

        buf = bytearray(min(alloc, 412))
        if fmt == 0 and len(buf) >= 20:
            buf[0:2] = struct.pack(">H", 18)
            buf[2:4] = bytes((1, 1))
            buf[4:8] = bytes((0, 20, 1, 0))
            buf[8:10] = bytes((0, 0))
            buf[10] = 2 if msf else 0
            buf[12:14] = bytes((0, 20))
            buf[14] = 0xAA
            if msf:
                buf[17], buf[18], buf[19] = mm, ss, ff
            else:
                packed = struct.pack(">I", num_sectors)
                buf[17:20] = packed[1:4]
        elif fmt == 1 and len(buf) >= 12:
            buf[0:2] = struct.pack(">H", 10)
            buf[2:4] = bytes((1, 1))
            buf[4:8] = bytes((0, 20, 1, 0))
            buf[8:10] = bytes((0, 0))
            buf[10] = 2 if msf else 0

        self._reply.set(SENSE_NO_SENSE, 0, 0, len(buf))
        self._send(bytes(buf))

    def _cdrom_mode_sense(self) -> None:
        payload = bytes((0, 8, 1, 0, 0, 0, 0, 0))
        self._reply.set(SENSE_NO_SENSE, 0, 0, len(payload))
        self._reply.set_media(payload[2])
        self._send(payload)

    def _cdrom_get_event_status(self, req: bytes) -> None:
        alloc = self._u16(req, 7)
        if (req[1] & 1) == 0:
            # The reference sends an illegal-request reply here and then falls
            # through to also send the polled-status reply below (a latent
            # double-reply in the original); preserved for wire fidelity.
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x24, 0, 0)
            self._send()
        if req[4] & 0x10:
            buf = bytearray(min(alloc, 8))
            if len(buf) >= 4:
                buf[0:4] = bytes((0, 6, 4, 16))
            self._reply.set(SENSE_NO_SENSE, 0, 0, len(buf))
            self._send(bytes(buf))
        else:
            buf = bytearray(min(alloc, 4))
            if len(buf) >= 4:
                buf[0:4] = bytes((0, 2, 0x80, 16))
            self._reply.set(SENSE_NO_SENSE, 0, 0, len(buf))
            self._send(bytes(buf))

    # -- floppy / USB key image (SCSIFloppy) ---------------------------------

    def _process_floppy(self, req: bytes) -> None:
        op = req[0]
        self._bump_sense_state()
        self._reply.set_media(36 if self.media.size() else 0)
        self._reply.set_flag(ReplyHeader.FLAG_WP, not self.writable)

        if op == SCSI_FORMAT_UNIT:
            self._floppy_format_unit(req)
        elif op == SCSI_PA_MEDIA_REMOVAL:
            self._floppy_pa_media_removal(req)
        elif op == SCSI_READ_CAPACITY:
            self._floppy_read_capacity()
        elif op == SCSI_SEND_DIAGNOSTIC:
            self._sense_state = 1
        elif op == SCSI_TEST_UNIT_READY:
            self._floppy_test_unit_ready()
        elif op in (SCSI_READ_10, SCSI_READ_12):
            self._floppy_read(req)
        elif op in (SCSI_WRITE_10, SCSI_WRITE_VERIFY, SCSI_WRITE_12):
            self._floppy_write(req)
        elif op == SCSI_READ_CAPACITIES:
            self._floppy_read_capacities()
        elif op == SCSI_START_STOP_UNIT:
            self._floppy_start_stop_unit(req)
        # Unhandled opcodes get no reply at all — matches the reference, which
        # only logs and returns without sending anything for this device type.

    def _floppy_test_unit_ready(self) -> None:
        if self._sense_state == 0:
            self._reply.set(SENSE_NOT_READY, 0x3A, 0, 0)
        elif self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, 0)
            self._sense_state = 2
        else:
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        self._send()

    def _floppy_read_capacity(self) -> None:
        payload = bytearray(8)
        if self._sense_state == 0:
            self._reply.set(SENSE_NOT_READY, 0x3A, 0, 0)
        elif self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, 0)
        else:
            last_lba = self.media.size() // 512 - 1
            payload[0:4] = struct.pack(">i", last_lba)
            payload[6] = 2
            self._reply.set(SENSE_NO_SENSE, 0, 0, len(payload))
        self._send(bytes(payload) if self._sense_state == 2 else b"")

    def _floppy_read_capacities(self) -> None:
        # [0,0,0,16, 0,0,2,0, 0,0,0,0, 0,0,0x0B,0x40, 0,0,2,0] — see reference
        # rcs_resp: current + one formattable-capacity descriptor.
        payload = bytearray(
            (0, 0, 0, 16, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0x0B, 0x40, 0, 0, 2, 0)
        )
        num_sectors = self.media.size() // 512
        payload[4:8] = struct.pack(">i", num_sectors)
        payload[10:12] = bytes((2, 0))
        if self._sense_state == 1:
            self._reply.set(SENSE_UNIT_ATTENTION, 0x28, 0, len(payload))
            self._sense_state = 2
        else:
            self._reply.set(SENSE_NO_SENSE, 0, 0, len(payload))
        self._send(bytes(payload))

    def _floppy_read(self, req: bytes) -> None:
        is_read12 = req[0] == 0xA8
        lba = self._u32(req, 2) * 512
        length = (self._u32(req, 6) if is_read12 else self._u16(req, 7)) * 512
        size = self.media.size()
        if 0 <= lba < size:
            try:
                data = self.media.read(lba, length)
                self._reply.set(SENSE_NO_SENSE, 0, 0, length)
                self._reply.set_flag(ReplyHeader.FLAG_WP, not self.writable)
                self._send(data)
                return
            except OSError:
                self._reply.set(SENSE_MEDIUM_ERROR, 0x10, 0, 0)
        else:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x21, 0, 0)
        self._send()

    def _floppy_write(self, req: bytes) -> None:
        is_write12 = req[0] == 0xAA
        lba = self._u32(req, 2) * 512
        length = (self._u32(req, 6) if is_write12 else self._u16(req, 7)) * 512
        data = self._read_complete(length) if length else b""
        if not self.writable:
            self._reply.set(SENSE_DATA_PROTECT, 0x27, 0, 0)
        elif 0 <= lba < self.media.size():
            try:
                self.media.write(lba, data)
                self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
            except OSError:
                self._reply.set(SENSE_MEDIUM_ERROR, 0x10, 0, 0)
        else:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x21, 0, 0)
        self._send()

    def _floppy_pa_media_removal(self, req: bytes) -> None:
        if req[4] & 1:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x24, 0, 0)
        else:
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        self._send()

    def _floppy_start_stop_unit(self, req: bytes) -> None:
        if req[4] & 2:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x24, 0, 0)
        else:
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        self._send()

    def _floppy_format_unit(self, req: bytes) -> None:
        alloc = self._u16(req, 7)
        params = self._read_complete(alloc) if alloc else b""
        recognized = False
        if len(params) >= 12:
            sectors = self._u32(params, 4)
            bytes_per_sector = self._u24(params, 9)
            recognized = (sectors, bytes_per_sector) in ((2880, 512), (1440, 512))
        if not self.writable:
            self._reply.set(SENSE_DATA_PROTECT, 0x27, 0, 0)
        elif recognized:
            # A fixed backing file can't actually be reformatted to a new
            # geometry; acknowledge success without resizing, matching the
            # observable effect for same-size images (the common case).
            self._reply.set(SENSE_NO_SENSE, 0, 0, 0)
        else:
            self._reply.set(SENSE_ILLEGAL_REQUEST, 0x26, 0, 0)
        self._send()


class VMediaMount:
    """A mounted image running its SCSI-target loop on a background thread."""

    def __init__(self, channel: VirtualMediaChannel) -> None:
        self.channel = channel
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Connect the channel and start its SCSI-target loop on a daemon thread."""
        self.channel.connect()
        self._thread = threading.Thread(
            target=self.channel.run,
            kwargs={"should_stop": self._stop.is_set},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the run loop to stop, close the channel, and close the media file."""
        self._stop.set()
        self.channel.close()
        self.channel.media.close()
