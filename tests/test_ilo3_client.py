"""Tests for ILoConsoleClient's disconnect handling.

Covers the failure mode reported live: the iLO server-side session times out
and closes the socket, and the client must (a) not crash on the input event
that races the drop, and (b) tell the UI the link is gone rather than sitting
silently in a stale "connected" state.
"""

from ilo3console.client import ILoConsoleClient
from ilo3console.console import ConsoleChannel


class DeadSock:
    """A socket that looks server-timed-out: recv errors, sendall too."""

    def __init__(self):
        self.closed = False

    def settimeout(self, t):
        pass

    def recv(self, n):
        raise ConnectionResetError()

    def sendall(self, data):
        raise BrokenPipeError()

    def close(self):
        self.closed = True


def _bare_client() -> ILoConsoleClient:
    # Build a client without going through connect() (no real network I/O).
    c = ILoConsoleClient("h", session_key="x" * 32)
    c.console = ConsoleChannel("h", 23, "x" * 32)
    c.console._sock = DeadSock()

    class NullDecoder:
        def feed(self, byte):
            pass

    c.decoder = NullDecoder()
    return c


def test_run_console_fires_on_disconnected_on_unexpected_drop():
    c = _bare_client()
    fired = []
    c.on_disconnected = lambda: fired.append(True)
    c._run_console()
    assert fired == [True]
    assert not c.console.connected


def test_run_console_does_not_fire_on_disconnected_after_deliberate_stop():
    c = _bare_client()
    fired = []
    c.on_disconnected = lambda: fired.append(True)
    c._stop.set()  # simulate a user-initiated close() having already run
    c._run_console()
    assert fired == []


def test_input_after_server_drop_does_not_raise():
    c = _bare_client()
    c._run_console()  # simulates the server timeout closing the socket
    assert not c.console.connected
    # A key-release racing the drop (e.g. a blur handler) must be a no-op.
    c.release_keys()
    c.send_ctrl_alt_del()
    c.send_power(0)
