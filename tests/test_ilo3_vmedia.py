"""Tests for the virtual-media SCSI-target channel."""

import struct

import pytest

from ilo3console import vmedia
from ilo3console.vmedia import (
    DEV_CDROM,
    DEV_FLOPPY,
    DEV_USBKEY,
    FileMedia,
    ReplyHeader,
    VirtualMediaChannel,
    VMediaError,
    pick_device_kind,
)

# -- ReplyHeader --------------------------------------------------------------


def test_reply_header_layout():
    h = ReplyHeader()
    h.set(5, 0x24, 0, 42)
    h.set_media(1)
    h.set_flag(ReplyHeader.FLAG_WP, True)
    data = h.to_bytes()
    assert len(data) == 16
    assert data[0:4] == bytes((0xDE, 0xC0, 0xAD, 0x0B))
    flags = struct.unpack_from("<i", data, 4)[0]
    assert flags == ReplyHeader.FLAG_WP
    assert data[8] == 1  # media
    assert data[9] == 5  # sense_key
    assert data[10] == 0x24  # asc
    assert data[11] == 0  # ascq
    assert struct.unpack_from("<i", data, 12)[0] == 42  # length


def test_reply_header_synch_echoes_request_bytes():
    h = ReplyHeader()
    req = bytes(range(12))  # req[4:12] = 4..11
    data = h.to_bytes_synch(req)
    assert data[0:4] == bytes((0xDE, 0xC0, 0xAD, 0x0B))
    assert data[8:16] == req[4:12]


# -- device kind selection ------------------------------------------------


def test_pick_device_kind_explicit():
    assert pick_device_kind("cdrom", 999) == DEV_CDROM
    assert pick_device_kind("floppy", 999) == DEV_FLOPPY
    assert pick_device_kind("usbkey", 1) == DEV_USBKEY


def test_pick_device_kind_auto_threshold():
    assert pick_device_kind("auto", vmedia.FLOPPY_USBKEY_THRESHOLD) == DEV_FLOPPY
    assert pick_device_kind("auto", vmedia.FLOPPY_USBKEY_THRESHOLD + 1) == DEV_USBKEY


def test_pick_device_kind_rejects_unknown():
    with pytest.raises(ValueError):
        pick_device_kind("floptical", 0)


# -- FileMedia --------------------------------------------------------------


def test_file_media_read_write_roundtrip(tmp_path):
    path = tmp_path / "disk.img"
    path.write_bytes(b"\x00" * 4096)
    m = FileMedia(str(path), writable=True)
    assert m.size() == 4096
    assert m.writable
    m.write(512, b"HELLO")
    assert m.read(512, 5) == b"HELLO"
    m.close()


def test_file_media_read_past_eof_is_zero_padded(tmp_path):
    path = tmp_path / "disk.img"
    path.write_bytes(b"\xAB" * 10)
    m = FileMedia(str(path))
    data = m.read(5, 10)
    assert data == b"\xAB" * 5 + b"\x00" * 5
    m.close()


def test_file_media_read_only_rejects_write(tmp_path):
    path = tmp_path / "disk.iso"
    path.write_bytes(b"\x00" * 2048)
    m = FileMedia(str(path), writable=False)
    assert not m.writable
    with pytest.raises(OSError):
        m.write(0, b"x")
    m.close()


# -- handshake ----------------------------------------------------------------


class FakeSock:
    def __init__(self, inbound: bytes = b""):
        self._in = bytearray(inbound)
        self.sent = bytearray()

    def setsockopt(self, *a):
        pass

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, n):
        chunk = bytes(self._in[:n])
        del self._in[:n]
        return chunk

    def close(self):
        pass


def _channel(tmp_path, kind="cdrom", size=2048, sock_inbound=b"", content=b""):
    path = tmp_path / "m.img"
    path.write_bytes(content + b"\x00" * (size - len(content)))
    media = FileMedia(str(path), writable=(kind != "cdrom"))
    ch = VirtualMediaChannel("h", 17988, "K" * 32, media, kind=kind)
    ch._sock = FakeSock(sock_inbound)
    return ch


def test_handshake_frame_device_and_key(tmp_path):
    ch = _channel(tmp_path, kind="cdrom")
    ch._sock = None
    # Build the frame the way connect() would, without actually opening a socket.
    import socket as socket_mod

    real_create_connection = socket_mod.create_connection
    sock = FakeSock(bytes((0x20, 0x00, 1, 0)))  # success response

    def fake_create_connection(addr, timeout=None):
        return sock

    socket_mod.create_connection = fake_create_connection
    try:
        ch.connect()
    finally:
        socket_mod.create_connection = real_create_connection

    assert sock.sent[0] == 0x10
    assert sock.sent[1] == (DEV_CDROM | 0x80)
    assert sock.sent[2:34] == b"K" * 32
    assert ch.connected


def test_handshake_rejected_raises(tmp_path):
    ch = _channel(tmp_path, kind="cdrom")
    import socket as socket_mod

    real_create_connection = socket_mod.create_connection
    sock = FakeSock(bytes((0x21, 0, 0, 0)))  # rejection

    socket_mod.create_connection = lambda addr, timeout=None: sock
    try:
        with pytest.raises(VMediaError):
            ch.connect()
    finally:
        socket_mod.create_connection = real_create_connection
    assert not ch.connected


# -- SCSI-target loop: CD-ROM ----------------------------------------------


def test_cdrom_test_unit_ready_then_read(tmp_path):
    ch = _channel(tmp_path, kind="cdrom", size=2048 * 4, content=b"ISO DATA")

    # First TEST_UNIT_READY transitions not-ready -> changed -> ready across
    # three calls (mirrors the reference's fdd_state progression).
    for _ in range(3):
        ch._process_cdrom(bytes([vmedia.SCSI_TEST_UNIT_READY] + [0] * 11))

    req = bytearray(12)
    req[0] = vmedia.SCSI_READ_10
    struct.pack_into(">I", req, 2, 0)  # LBA 0
    struct.pack_into(">H", req, 7, 1)  # 1 block

    sock = FakeSock()
    ch._sock = sock
    ch._process_cdrom(bytes(req))

    header = bytes(sock.sent[:16])
    assert header[0:4] == bytes((0xDE, 0xC0, 0xAD, 0x0B))
    length = struct.unpack_from("<i", header, 12)[0]
    assert length == 2048
    payload = bytes(sock.sent[16 : 16 + length])
    assert payload.startswith(b"ISO DATA")


def test_cdrom_read_out_of_range_reports_illegal_request(tmp_path):
    ch = _channel(tmp_path, kind="cdrom", size=2048)
    for _ in range(3):
        ch._process_cdrom(bytes([vmedia.SCSI_TEST_UNIT_READY] + [0] * 11))

    req = bytearray(12)
    req[0] = vmedia.SCSI_READ_10
    struct.pack_into(">I", req, 2, 999)  # way past the 1-block image
    struct.pack_into(">H", req, 7, 1)

    sock = FakeSock()
    ch._sock = sock
    ch._process_cdrom(bytes(req))
    header = bytes(sock.sent[:16])
    assert header[9] == vmedia.SENSE_ILLEGAL_REQUEST


# -- SCSI-target loop: floppy/USB key ---------------------------------------


def test_floppy_write_then_read_roundtrip(tmp_path):
    ch = _channel(tmp_path, kind="floppy", size=512 * 8)
    for _ in range(3):
        ch._process_floppy(bytes([vmedia.SCSI_TEST_UNIT_READY] + [0] * 11))

    payload = b"X" * 512
    write_req = bytearray(12)
    write_req[0] = vmedia.SCSI_WRITE_10
    struct.pack_into(">I", write_req, 2, 1)  # LBA 1
    struct.pack_into(">H", write_req, 7, 1)

    sock = FakeSock(payload)  # data the initiator "sends" after the CDB
    ch._sock = sock
    ch._process_floppy(bytes(write_req))
    assert bytes(sock.sent[:16])[9] == vmedia.SENSE_NO_SENSE

    read_req = bytearray(12)
    read_req[0] = vmedia.SCSI_READ_10
    struct.pack_into(">I", read_req, 2, 1)
    struct.pack_into(">H", read_req, 7, 1)
    sock2 = FakeSock()
    ch._sock = sock2
    ch._process_floppy(bytes(read_req))
    header = bytes(sock2.sent[:16])
    length = struct.unpack_from("<i", header, 12)[0]
    assert bytes(sock2.sent[16 : 16 + length]) == payload


def test_floppy_write_rejected_when_read_only(tmp_path):
    ch = _channel(tmp_path, kind="cdrom", size=512 * 4)  # cdrom => not writable
    ch.is_cdrom = False  # force the floppy code path with a read-only backing
    ch.block_size = 512
    for _ in range(3):
        ch._process_floppy(bytes([vmedia.SCSI_TEST_UNIT_READY] + [0] * 11))

    write_req = bytearray(12)
    write_req[0] = vmedia.SCSI_WRITE_10
    struct.pack_into(">I", write_req, 2, 0)
    struct.pack_into(">H", write_req, 7, 1)
    sock = FakeSock(b"Y" * 512)
    ch._sock = sock
    ch._process_floppy(bytes(write_req))
    header = bytes(sock.sent[:16])
    assert header[9] == vmedia.SENSE_DATA_PROTECT


# -- sync-echo ping -----------------------------------------------------------


def test_sync_echo_is_answered_and_not_treated_as_a_command(tmp_path):
    ch = _channel(tmp_path, kind="cdrom")
    ping = (bytes([vmedia.SYNC_ECHO_CMD]) + bytes(range(11)))[:12]
    real_cmd = bytearray(12)
    real_cmd[0] = vmedia.SCSI_TEST_UNIT_READY

    ch._sock = FakeSock(ping + bytes(real_cmd))
    req = ch._read_command()
    assert req == bytes(real_cmd)
    # The sync ping must have been echoed before the real command was returned.
    echoed = bytes(ch._sock.sent[:16])
    assert echoed[0:4] == bytes((0xDE, 0xC0, 0xAD, 0x0B))
    assert echoed[8:16] == ping[4:12]
