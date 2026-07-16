"""High-level orchestration: one call to open a full iLO 3 console session.

Ties together the HTTPS session, the console channel + DVC decoder, and the
command channel, wiring the decoder's side-band callbacks back to the console
socket (cipher switch, ack, refresh). Runs the two receive loops in background
threads and exposes a small input API plus the live :class:`Framebuffer`.

Typical use::

    client = ILoConsoleClient("ilo.example", username="admin", password="…")
    client.connect()
    client.on_frame = lambda: push(client.framebuffer)
    client.send_ctrl_alt_del()
    ...
    client.close()
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from . import input as inp
from .cmdchannel import CmdChannel, CmdController
from .console import CONNECT_SHARE, ConsoleChannel
from .dvc import DvcController, DvcDecoder, Framebuffer
from .session import ILoSession, RcInfo
from .vmedia import FileMedia, VirtualMediaChannel, VMediaMount


class ILoConsoleClient:
    """A live remote-console session against one iLO 3.

    Authenticate with ``username``/``password`` (the client mints the token) or a
    pre-minted ``session_key``. Set the ``on_*`` callback attributes to react to
    video frames and status changes.
    """

    def __init__(
        self,
        host: str,
        *,
        username: str | None = None,
        password: str | None = None,
        session_key: str | None = None,
        port: int = 443,
        connect_mode: int = CONNECT_SHARE,
    ) -> None:
        if session_key is None and not (username and password):
            raise ValueError("provide either session_key or username+password")
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._session_key = session_key
        self._connect_mode = connect_mode

        self.session: ILoSession | None = None
        self.rc_info: RcInfo | None = None
        self.console: ConsoleChannel | None = None
        self.cmd: CmdChannel | None = None
        self.framebuffer = Framebuffer()
        self.decoder: DvcDecoder | None = None

        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._vmedia: dict[str, VMediaMount] = {}  # "FLOPPY"/"CDROM" -> mount

        # User-settable callbacks (all optional).
        self.on_frame: Callable[[], None] = lambda: None
        self.on_power: Callable[[bool], None] = lambda on: None
        self.on_health: Callable[[int], None] = lambda level: None
        self.on_status: Callable[[int, str], None] = lambda field, text: None
        self.on_text: Callable[[str], None] = lambda text: None
        self.on_resize: Callable[[int, int], None] = lambda w, h: None
        self.on_seize: Callable[[], None] = lambda: None
        self.on_cipher: Callable[[int], None] = lambda cipher: None
        self.on_disconnected: Callable[[], None] = lambda: None

    # -- lifecycle ------------------------------------------------------------

    def connect(self) -> None:
        """Authenticate, open both channels, and start the receive loops."""
        if self._session_key is not None:
            self.session = ILoSession.from_token(
                self.host, self._session_key, self.port
            )
        else:
            self.session = ILoSession.login(
                self.host, self._username, self._password, self.port
            )
        self.rc_info = self.session.rc_info()
        rc = self.rc_info

        self.console = ConsoleChannel(
            self.host,
            rc.rc_port,
            rc.session_key,
            enc_key_bytes=rc.enc_key_val,
            enc_key_hex=rc.enc_key,
            optional_features=rc.optional_features,
        )
        self.console.connect(self._connect_mode)

        self.decoder = DvcDecoder(self.framebuffer, _VideoBridge(self))

        self.cmd = CmdChannel(
            self.host,
            rc.rc_port,
            rc.session_key,
            _CmdBridge(self),
            enc_key_hex=rc.enc_key,
            optional_features=rc.optional_features,
        )
        self.cmd.connect()

        self._spawn(self._run_console)
        self._spawn(self._run_cmd)

    def _spawn(self, target) -> None:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._threads.append(t)

    def _run_console(self) -> None:
        self.console.run_receiver(self.decoder, should_stop=self._stop.is_set)
        # Loop ended on its own (EOF / socket error), not via close() — the link
        # to the iLO dropped out from under us; tell whoever's listening.
        if not self._stop.is_set():
            self.on_disconnected()

    def _run_cmd(self) -> None:
        self.cmd.run(should_stop=self._stop.is_set)

    def close(self) -> None:
        """Stop the receive threads, unmount all virtual media, and log out."""
        self._stop.set()
        for mount in self._vmedia.values():
            mount.stop()
        self._vmedia.clear()
        if self.console:
            self.console.close()
        if self.cmd:
            self.cmd.close()
        if self.session:
            self.session.logout()

    def __enter__(self) -> ILoConsoleClient:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- input API ------------------------------------------------------------

    def _send(self, frame: bytes) -> None:
        if self.console is None:
            raise RuntimeError("not connected")
        self.console.send(frame)

    def send_key_report(self, modifiers: int, keys=()) -> None:
        """Send a raw HID keyboard report (held modifiers + up to 6 keys)."""
        self._send(inp.keyboard_report(modifiers, keys))

    def release_keys(self) -> None:
        """Send an all-keys-up HID report (e.g. on keyup or focus loss)."""
        self._send(inp.keyboard_release())

    def send_combo(self, frames: list[bytes]) -> None:
        """Send a sequence of pre-built input frames in order (e.g. a key combo)."""
        for frame in frames:
            self._send(frame)

    def send_ctrl_alt_del(self) -> None:
        """Send the Ctrl+Alt+Del key-down/key-up frame sequence."""
        self.send_combo(inp.ctrl_alt_del())

    def send_power(self, option: int) -> None:
        """``option`` is one of ``input.POWER_*``."""
        self._send(inp.power(option))

    def send_mouse(
        self, x: int, y: int, buttons: int = 0, dx: int = 0, dy: int = 0
    ) -> None:
        """Send an absolute-position mouse report scaled to the current
        framebuffer size.
        """
        self._send(
            inp.mouse_absolute(
                x, y, self.framebuffer.width, self.framebuffer.height, dx, dy, buttons
            )
        )

    def refresh_screen(self) -> None:
        """Ask the iLO to resend a full video frame."""
        self._send(inp.refresh_screen())

    # -- virtual media (local file, SCSI-target channel) -----------------------

    def mount_local_image(self, path: str, kind: str = "auto") -> None:
        """Mount a local disk image or ISO on the iLO as virtual media.

        ``kind`` is ``"cdrom"`` (read-only ISO), ``"floppy"``, ``"usbkey"``, or
        ``"auto"`` (floppy vs. USB key chosen by image size — see
        docs/PROTOCOL_ILO3.md). Requires ``rc_info.vm_port`` — set after
        :meth:`connect`. Replaces whatever is already mounted on that slot.
        """
        if self.rc_info is None or self.rc_info.vm_port is None:
            raise RuntimeError("not connected, or iLO did not report a vm_port")
        slot = "CDROM" if kind == "cdrom" else "FLOPPY"
        self.unmount_local_image(slot)
        media = FileMedia(path, writable=kind != "cdrom")
        channel = VirtualMediaChannel(
            self.host,
            self.rc_info.vm_port,
            self.rc_info.session_key,
            media,
            kind=kind,
            enc_key_hex=self.rc_info.enc_key,
            optional_features=self.rc_info.optional_features,
        )
        mount = VMediaMount(channel)
        mount.start()
        self._vmedia[slot] = mount

    def unmount_local_image(self, slot: str) -> None:
        """Unmount whatever local image is attached to ``"FLOPPY"``/``"CDROM"``."""
        mount = self._vmedia.pop(slot.upper(), None)
        if mount is not None:
            mount.stop()

    # -- virtual media (URL, iLO fetches it directly) --------------------------

    def insert_url_media(self, device: str, url: str) -> str:
        """Point the iLO at an HTTP(S) image for ``"FLOPPY"``/``"CDROM"``.

        Returns ``"Success"`` or the iLO's error string.
        """
        if self.session is None:
            raise RuntimeError("not connected")
        return self.session.insert_url_media(device.upper(), url)

    def eject_url_media(self, device: str) -> str:
        """Eject the URL-based virtual media currently attached to ``device``."""
        if self.session is None:
            raise RuntimeError("not connected")
        return self.session.eject_url_media(device.upper())

    def vm_status(self):
        """Return the iLO's current virtual-media status for both device slots."""
        if self.session is None:
            raise RuntimeError("not connected")
        return self.session.vm_status()


class _VideoBridge(DvcController):
    """Routes DVC decoder callbacks to the console socket and client callbacks."""

    def __init__(self, client: ILoConsoleClient) -> None:
        self._c = client

    def set_cipher(self, cipher: int) -> None:
        self._c.console.set_decrypt_cipher(cipher)
        self._c.on_cipher(cipher)

    def send_ack(self) -> None:
        self._c.console.send(inp.ack())

    def refresh_screen(self) -> None:
        self._c.console.send(inp.refresh_screen())

    def on_power(self, on: bool) -> None:
        self._c.on_power(on)

    def on_status(self, field: int, text: str) -> None:
        self._c.on_status(field, text)

    def on_text(self, text: str) -> None:
        self._c.on_text(text)

    def on_resize(self, width: int, height: int) -> None:
        self._c.on_resize(width, height)

    def on_frame(self) -> None:
        self._c.on_frame()

    def on_seize(self) -> None:
        self._c.on_seize()


class _CmdBridge(CmdController):
    """Routes command-channel events to the client's callbacks."""

    def __init__(self, client: ILoConsoleClient) -> None:
        self._c = client

    def on_power(self, on: bool) -> None:
        self._c.on_power(on)

    def on_health(self, level: int) -> None:
        self._c.on_health(level)

    def on_seize_request(self, user: str, host: str) -> bool:
        self._c.on_seize()
        return False
