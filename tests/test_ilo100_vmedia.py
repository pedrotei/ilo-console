"""Tests for the iLO 100 virtual-media data channel (vmedia100.py).

The outer frame format (_REQUEST_SIZE/_REPLY_SIZE/hello handshake) is
documented in docs/PROTOCOL_ILO100.md; the SCSI CDB semantics match what
ilo3console.vmedia already implements byte-for-byte. These tests exercise
the frame parsing and SCSI command handling directly, without a real
socket.
"""

import struct

import pytest

from ilo100console.vmedia100 import (
    _REQUEST_SIZE,
    SCSI_READ_10,
    SCSI_TEST_UNIT_READY,
    FileMedia,
    MediaChannel,
    VMediaError,
)


class FakeConn:
    """Replays canned bytes for recv(); records what's sent."""

    def __init__(self, inbound: bytes = b""):
        self._in = bytearray(inbound)
        self.sent = bytearray()

    def recv(self, n):
        chunk = bytes(self._in[:n])
        del self._in[:n]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        pass


def _request(opcode: int, *cdb_rest: int) -> bytes:
    cdb = bytes([opcode, *cdb_rest]).ljust(12, b"\x00")
    return bytes(_REQUEST_SIZE - 12) + cdb


def _hello_bytes() -> bytes:
    header = bytes(8)
    tag = b"VENDORID"
    rest = bytes(536 - 8 - 8)
    ready_ping = bytes(8)
    return header + tag + rest + ready_ping


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "test.iso"
    data = bytearray(4096)
    data[2048 : 2048 + 5] = b"HELLO"
    path.write_bytes(bytes(data))
    return FileMedia(str(path))


def test_hello_handshake_sends_16_byte_capability_reply(media):
    conn = FakeConn(_hello_bytes())
    channel = MediaChannel(conn, media)
    channel._read_hello()
    assert len(conn.sent) == 16


def test_hello_handshake_rejects_wrong_tag(media):
    bad_hello = bytes(8) + b"NOTAVEND" + bytes(536 - 16) + bytes(8)
    conn = FakeConn(bad_hello)
    channel = MediaChannel(conn, media)
    with pytest.raises(VMediaError):
        channel._read_hello()


def test_test_unit_ready_replies_with_empty_status(media):
    conn = FakeConn(_request(SCSI_TEST_UNIT_READY))
    channel = MediaChannel(conn, media)
    assert channel._serve_one_request() is True
    assert len(conn.sent) == 64
    length_field = struct.unpack_from("<I", conn.sent, 24)[0]
    assert length_field == 0


def test_read_10_returns_correct_block_data(media):
    # LBA=1, length=1 block (2048 bytes) -- media has "HELLO" at that offset.
    cdb = (
        bytes([SCSI_READ_10, 0])
        + struct.pack(">I", 1)  # LBA
        + bytes([0])  # group number
        + struct.pack(">H", 1)  # transfer length (blocks)
        + bytes([0])  # control
    ).ljust(12, b"\x00")
    conn = FakeConn(bytes(_REQUEST_SIZE - 12) + cdb)
    channel = MediaChannel(conn, media)
    assert channel._serve_one_request() is True
    length_field = struct.unpack_from("<I", conn.sent, 24)[0]
    assert length_field == 2048
    data = bytes(conn.sent[64:])
    assert len(data) == 2048
    assert data[:5] == b"HELLO"


def test_read_10_beyond_eof_zero_pads(media):
    cdb = (
        bytes([SCSI_READ_10, 0])
        + struct.pack(">I", 1000)
        + bytes([0])
        + struct.pack(">H", 1)
        + bytes([0])
    ).ljust(12, b"\x00")
    conn = FakeConn(bytes(_REQUEST_SIZE - 12) + cdb)
    channel = MediaChannel(conn, media)
    channel._serve_one_request()
    data = bytes(conn.sent[64:])
    assert len(data) == 2048
    assert data == bytes(2048)


def test_serve_one_request_returns_false_on_short_read(media):
    conn = FakeConn(b"\x00" * 10)  # less than a full 76-byte request
    channel = MediaChannel(conn, media)
    assert channel._serve_one_request() is False
