"""Tests for the console handshake logic and keystream synchronisation."""

from ilo3console import console
from ilo3console.crypto import CIPHER_RC4, RC4Keystream


class FakeSock:
    """A scripted socket: replays ``inbound`` for recv, records sends."""

    def __init__(self, inbound: bytes):
        self._in = bytearray(inbound)
        self.sent = bytearray()
        self.closed = False

    def recv(self, n: int) -> bytes:
        chunk = bytes(self._in[:n])
        del self._in[:n]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def settimeout(self, t) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class BrokenPipeSock(FakeSock):
    """Simulates the server having dropped the connection: writes fail."""

    def sendall(self, data: bytes) -> None:
        raise BrokenPipeError()


class DeadSock(FakeSock):
    """Simulates a server-side timeout: recv raises (connection reset)."""

    def recv(self, n: int) -> bytes:
        raise ConnectionResetError()


def _channel(**kw):
    return console.ConsoleChannel("h", 23, "A" * 32, **kw)


def test_xor_session_key_no_obfuscation():
    assert console.xor_session_key("ABCD", "00", False) == b"ABCD"


def test_xor_session_key_obfuscation_is_reversible():
    key = "deadbeef"
    out = console.xor_session_key("ABCD", key, True)
    assert out != b"ABCD"
    # XOR twice with the same key material returns the original.
    back = bytearray(out)
    for i in range(len(back)):
        back[i] ^= ord(key[i % len(key)])
    assert bytes(back) == b"ABCD"


def test_connect_request_command_bytes():
    ch = _channel()
    req = ch._connect_request(0x2001)
    assert req[0] == 0x01 and req[1] == 0x20
    assert req[2:] == b"A" * 32  # no obfuscation without ENCRYPT_KEY


def test_connect_request_encrypt_key_flag():
    ch = _channel(optional_features="ENCRYPT_KEY", enc_key_hex="deadbeef")
    req = ch._connect_request(0x2001)
    assert req[1] == (0x20 | 0x80)  # ENCRYPT_KEY without VMKEY sets 0x80
    assert req[2:] != b"A" * 32  # token obfuscated


def test_auth_response_success():
    ch = _channel()
    ch._sock = FakeSock(bytes([console.RESP_AUTH]))
    ch._handle_auth_response(console.CONNECT_SHARE)
    assert ch.authenticated and ch.licensed


def test_auth_response_busy_then_seize_granted():
    ch = _channel()
    ch._sock = FakeSock(bytes([console.RESP_BUSY_A, console.RESP_AUTH]))
    ch._handle_auth_response(console.CONNECT_SEIZE)
    assert ch.authenticated
    # Client must have sent the seize command word 0x0055.
    assert bytes(ch._sock.sent) == bytes([0x55, 0x00])


def test_auth_response_denied_raises():
    ch = _channel()
    ch._sock = FakeSock(bytes([console.RESP_DENIED]))
    try:
        ch._handle_auth_response(console.CONNECT_SHARE)
    except console.HandshakeError:
        pass
    else:
        raise AssertionError("expected HandshakeError on denial")


def test_send_encrypts_when_active():
    ch = _channel(enc_key_bytes=bytes(range(1, 17)))
    ch.set_decrypt_cipher(CIPHER_RC4)
    assert ch.dvc_encryption
    ch._sock = FakeSock(b"")
    ch.send(bytes([0x00, 0x00]))
    # The sent bytes must equal plaintext XOR the RC4 keystream.
    ks = RC4Keystream(bytes(range(1, 17)))
    expected = bytes([0x00 ^ ks.random_value(), 0x00 ^ ks.random_value()])
    assert bytes(ch._sock.sent) == expected


def test_send_after_close_is_a_safe_noop():
    ch = _channel()
    ch._sock = FakeSock(b"")
    ch.close()
    ch.send(bytes([1, 2, 3]))  # must not raise


def test_send_on_broken_pipe_closes_instead_of_raising():
    ch = _channel()
    ch._sock = BrokenPipeSock(b"")
    ch.send(bytes([1, 2, 3]))  # must not raise
    assert not ch.connected

    # A second send (e.g. a late blur-triggered key-release) is now a no-op.
    ch.send(bytes([4, 5, 6]))


def test_connected_reflects_socket_state():
    ch = _channel()
    assert not ch.connected
    ch._sock = FakeSock(b"")
    assert ch.connected
    ch.close()
    assert not ch.connected


class _NullSink:
    def feed(self, byte: int) -> None:
        pass


def test_run_receiver_closes_on_server_reset():
    ch = _channel()
    sock = DeadSock(b"")
    ch._sock = sock
    ch.run_receiver(_NullSink())
    assert not ch.connected
    assert sock.closed


def test_run_receiver_closes_on_eof():
    ch = _channel()
    sock = FakeSock(b"")  # recv() immediately returns b"" (EOF)
    ch._sock = sock
    ch.run_receiver(_NullSink())
    assert not ch.connected
    assert sock.closed
