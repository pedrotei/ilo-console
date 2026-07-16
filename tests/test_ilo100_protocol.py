"""Wire-format tests for the iLO 100 / Mahogany protocol layer."""

import struct

import pytest

from ilo100console import protocol as p
from ilo100console.buffer import SocketReader


class FakeSocket:
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


def _reader(data: bytes) -> SocketReader:
    return SocketReader(FakeSocket(data))


def test_client_handshake_framing():
    frame = p.encode_client_handshake("bob", "hunter2", config_mask=p.CFG_VIDEO)
    assert frame[0] == p.MSG_CLIENT_HANDSHAKE
    sig, ulen, plen, cfglen, keylen = struct.unpack_from("<iiiii", frame, 1)
    # Default signature matches a live capture of the iLO's own web console
    # (which always sends the embedded default, never the standalone one).
    assert sig == p.HANDSHAKE_SIG_EMBEDDED
    assert (ulen, plen, cfglen, keylen) == (16, 20, 4, 0)
    off = 1 + 20
    assert frame[off : off + 16] == b"bob" + b"\x00" * 13
    off += 16
    assert frame[off : off + 20] == b"hunter2" + b"\x00" * 13
    off += 20
    cfg = struct.unpack_from("<i", frame, off)[0]
    assert cfg == p.CFG_VIDEO
    off += 4  # + 0 key bytes
    assert frame[off : off + 128] == b"hunter2" + b"\x00" * 121


def test_client_handshake_truncates_long_credentials():
    frame = p.encode_client_handshake("a" * 30, "b" * 30)
    off = 1 + 20
    assert frame[off : off + 16] == b"a" * 16
    off += 16
    assert frame[off : off + 20] == b"b" * 20


def test_mouse_move_and_key_state_change():
    assert p.encode_mouse_move(10, -5) == bytes([p.MSG_MOUSE_MOVE]) + struct.pack(
        "<ii", 10, -5
    )
    frame = p.encode_key_state_change(0x04, True, pre_translated=False)
    assert frame == bytes([p.MSG_KEY_STATE_CHANGE, 0x04, 0x00, 0x01, 0x00])


def test_button_state_at_absolute_layout():
    frame = p.encode_button_state_at_absolute(
        100, 200, left=True, right=False, middle=True
    )
    assert frame[0] == p.MSG_BUTTON_STATE_AT_ABSOLUTE
    x, y = struct.unpack_from("<ii", frame, 1)
    assert (x, y) == (100, 200)
    assert frame[9] == 3  # button count
    left, right, middle = frame[10], frame[11], frame[12]
    assert left & 1 == 1 and right & 1 == 0 and middle & 1 == 1
    # Unpressed buttons keep the "centered trackwheel" high bit (0x80).
    assert right == 0x80


def test_client_nop_and_simple_messages():
    # 8 bytes total (id + 7-byte zero payload) — verified against a live
    # capture; a bare 1-byte frame here desyncs every message sent after it.
    assert p.encode_client_nop() == bytes([p.MSG_CLIENT_NOP]) + b"\x00" * 7
    assert p.encode_request_vesa_mode() == bytes([p.MSG_REQUEST_VESA_MODE])
    assert p.encode_request_primary_control() == bytes([p.MSG_REQUEST_PRIMARY_CONTROL])
    assert p.encode_client_absolute_mode(True) == bytes([p.MSG_CLIENT_ABSOLUTE_MODE, 1])
    assert p.encode_client_relative_mode(True, False) == bytes(
        [p.MSG_CLIENT_RELATIVE_MODE, 1, 0]
    )


def test_client_disconnect_message():
    frame = p.encode_client_disconnect(p.DISCONNECT_EXIT_SUCCESS, "bye")
    assert frame[0] == p.MSG_CLIENT_DISCONNECT
    reason = struct.unpack_from("<i", frame, 1)[0]
    assert reason == p.DISCONNECT_EXIT_SUCCESS
    n = struct.unpack_from("<h", frame, 5)[0]
    assert n == 3
    assert frame[7:10] == b"bye"


# -- decoders -----------------------------------------------------------


def test_decode_server_handshake_flags():
    # 32-byte buffer: a session id, a space, then single-letter privilege
    # flags (k/v/m; no 's' — must NOT be misread from a longer word like
    # "mouse", which itself contains the letter 's').
    raw = b"SESS kvm"[:32].ljust(32, b"\x00")
    r = _reader(raw)
    hs = p.decode_server_handshake(r)
    assert hs.keyboard and hs.video and hs.mouse
    assert not hs.storage


def test_decode_server_handshake_storage_flag():
    raw = b"SESS kvms"[:32].ljust(32, b"\x00")
    r = _reader(raw)
    hs = p.decode_server_handshake(r)
    assert hs.storage


def test_decode_inform_vesa_mode():
    payload = struct.pack("<hhhh", 0, 1024, 768, 16)
    r = _reader(payload)
    m = p.decode_inform_vesa_mode(r)
    assert (m.mode_number, m.width, m.height, m.bpp) == (0, 1024, 768, 16)


def test_decode_bit_blt_roundtrip():
    src = struct.pack("<hhhh", 0, 0, 4, 4)
    dst = struct.pack("<hhhh", 1, 2, 4, 4)
    payload = struct.pack("<hBB", p.BLT_COPY, 0, 0) + src + dst
    data = bytes(range(16))
    payload += struct.pack("<i", len(data)) + data
    r = _reader(payload)
    blt = p.decode_bit_blt(r)
    assert blt.blit_type == p.BLT_COPY
    assert blt.dest == p.Region(1, 2, 4, 4)
    assert blt.data == data


def test_decode_set_text_cursor_swaps_reversed_lines():
    payload = bytes([5, 6, 10, 3])  # start(10) > stop(3) -> must swap
    r = _reader(payload)
    c = p.decode_set_text_cursor(r)
    assert (c.x, c.y, c.start_line, c.stop_line) == (5, 6, 3, 10)


def test_decode_special_graphics_bit():
    # blinking=1, swap=0, primary_select=2, secondary_select=0, bpp4_idx=1
    value = 1 | (2 << 2) | (1 << 8)
    r = _reader(struct.pack("<i", value))
    sg = p.decode_special_graphics_bit(r)
    assert sg.blinking_text is True
    assert sg.is_swapping is False
    assert sg.primary_select_offset == 1024  # CHARACTER_MAP_OFFSET[2]
    assert sg.bpp4_color_index == 1


def test_decode_server_disconnect():
    msg = "bye now"
    payload = struct.pack("<i", 2) + struct.pack("<h", len(msg)) + msg.encode()
    r = _reader(payload)
    d = p.decode_server_disconnect(r)
    assert d.reason == 2
    assert d.message == msg


def test_decode_set_palette_and_apply_to_video_state():
    from ilo100console.video import VideoState

    attrs = struct.pack("<h", 1) + bytes([3, 9])  # index 3 -> value 9
    pal = struct.pack("<h", 1) + bytes([7, 10, 20, 30])  # offset7 -> (r,g,b)=(10,20,30)
    r = _reader(attrs + pal)
    sp = p.decode_set_palette(r)
    vs = VideoState()
    vs.set_palette(sp)
    assert vs.attributes[3] == 9
    assert (vs.pal_r[7], vs.pal_g[7], vs.pal_b[7]) == (10, 20, 30)


def test_httpdata_token_regex_extracts_from_kvms_html():
    from ilo100console.session import _HTTPDATA_RE

    html = (
        '<PARAM NAME="NonSecure_KVMPort" VALUE="80"> '
        '<PARAM NAME="httpdata" VALUE="BAED17A9F9C6CE55" > '
        '<PARAM NAME="username" VALUE="admin" >'
    )
    m = _HTTPDATA_RE.search(html)
    assert m is not None
    assert m.group(1) == "BAED17A9F9C6CE55"


def test_httpdata_token_regex_no_match_returns_none():
    from ilo100console.session import _HTTPDATA_RE

    assert _HTTPDATA_RE.search("<html>Unauthorized. Please Log In.</html>") is None


def test_fetch_httpdata_token_retries_transient_reset(monkeypatch):
    # This BMC's httpd is known to reset mid-response intermittently; a
    # read-only GET should retry through that rather than fail outright.
    from ilo100console import session

    monkeypatch.setattr(session, "_TRANSIENT_RETRY_DELAYS", (0, 0, 0))
    attempts = []
    html = '<PARAM NAME="httpdata" VALUE="CAFEBABE" >'

    def flaky_fetch(url, opener, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise session._TransientConnectionError("connection reset")
        return html

    monkeypatch.setattr(session, "_fetch_kvms_html", flaky_fetch)
    token = session.fetch_httpdata_token("host.invalid", "admin", "pw")
    assert token == "CAFEBABE"
    assert len(attempts) == 3


def test_encode_storage_client_connect_layout():
    frame = p.encode_storage_client_connect(
        bytes([192, 168, 1, 69]), 44953, 0, p.MEDIA_TYPE_CD_ISO_IMAGE, "test.iso",
    )
    assert frame[0] == p.MSG_STORAGE_CLIENT_CONNECT
    body = frame[1:]
    # IP right-aligned in a 16-byte field (StorageClientConnect.setIPAddress).
    assert body[0:16] == bytes(12) + bytes([192, 168, 1, 69])
    assert body[16] == 0  # share_index0
    assert body[17] == 0xFF  # share_index1 (-1, undefined)
    port = struct.unpack_from("<h", body, 18)[0]
    assert port == 44953 - 0x10000  # signed16 wraparound for an ephemeral port
    assert body[20] == len("test.iso")  # share_path0 length in characters
    assert body[21] == 0  # share_path1 length
    assert body[22] == p.MEDIA_TYPE_CD_ISO_IMAGE  # share_type0
    assert body[23] == 0xFF  # share_type1 (-1, undefined)
    assert body[24] == p.IP_TYPE_IPV4
    path0_start = 25 + 7  # + 7 reserved bytes
    path0 = body[path0_start: path0_start + 512]
    assert path0[: len("test.iso") * 2] == "test.iso".encode("utf-16-le")
    assert path0[len("test.iso") * 2 :] == bytes(512 - len("test.iso") * 2)
    assert len(frame) == 1 + 16 + 1 + 1 + 2 + 1 + 1 + 1 + 1 + 1 + 7 + 512 + 512


def test_encode_storage_client_disconnect():
    assert p.encode_storage_client_disconnect() == bytes(
        [p.MSG_STORAGE_CLIENT_DISCONNECT, 0]
    )


def test_decode_storage_status_roundtrip():
    ip = bytes(12) + bytes([192, 168, 1, 69])
    path0 = "test.iso"
    path0_bytes = path0.encode("utf-16-le").ljust(512, b"\x00")
    path1_bytes = bytes(512)
    payload = (
        ip
        + bytes([0, 0xFF])  # share_index0/1
        + struct.pack("<H", 5901)  # port
        + bytes([len(path0), 0])  # name lengths
        + bytes([p.MEDIA_TYPE_CD_ISO_IMAGE, 0xFF])  # share types
        + bytes([p.STORAGE_STATUS_CONNECTED, p.STORAGE_STATUS_NOT_CONNECTED])
        + bytes([p.IP_TYPE_IPV4])
        + bytes(5)  # reserved
        + path0_bytes
        + path1_bytes
    )
    r = _reader(payload)
    status = p.decode_storage_status(r)
    assert status.share_status0 == p.STORAGE_STATUS_CONNECTED
    assert status.share_path0 == "test.iso"
    assert status.share_path1 == ""
    assert status.port == 5901


def test_multi_user_state_allowed_to_do_storage():
    mus_allowed = p.MultiUserState(flags=0x100)
    assert mus_allowed.allowed_to_do_storage is True
    mus_not_allowed = p.MultiUserState(flags=0x101EEF)  # observed live, bit 0x100 clear
    assert mus_not_allowed.allowed_to_do_storage is False


def test_fetch_httpdata_token_does_not_retry_auth_failure(monkeypatch):
    from ilo100console import session

    monkeypatch.setattr(session, "_TRANSIENT_RETRY_DELAYS", (0, 0, 0))
    attempts = []

    def bad_auth(url, opener, timeout):
        attempts.append(1)
        raise session.HandshakeError("web login failed: HTTP 401")

    monkeypatch.setattr(session, "_fetch_kvms_html", bad_auth)
    with pytest.raises(session.HandshakeError):
        session.fetch_httpdata_token("host.invalid", "admin", "wrongpw")
    assert len(attempts) == 1  # fails fast, no retry on a real auth failure


def test_multi_user_state_view_only_flags():
    # Live-captured flags from a freshly connected, uncontested session: the
    # server defaults every new connection to view-only (user_status == 1)
    # even though nothing else is connected -- allowed_to_take_full_control
    # is the signal that it's safe to auto-request control (see client.py's
    # _apply_multi_user_state, which mirrors the real console client's
    # "request full control?" dialog that a human would otherwise have to
    # click OK on).
    mus = p.MultiUserState(flags=0x101EEF)
    assert mus.user_status == 1
    assert mus.has_full_control is False
    assert mus.allowed_to_take_full_control is True


def test_multi_user_state_full_control_flags():
    # same flags with USER_STATUS_MASK bits cleared
    mus = p.MultiUserState(flags=0x101EE7)
    assert mus.user_status == 0
    assert mus.has_full_control is True


def test_decode_multi_user_state_flags_field():
    payload = struct.pack("<i", 0x101EEF) + bytes([0, 0]) + bytes([0, 0])
    r = _reader(payload)
    mus = p.decode_multi_user_state(r)
    assert mus.flags == 0x101EEF
    assert mus.user_status == 1
