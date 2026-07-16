"""High-level orchestration: one call to open a full iLO 100 KVM session.

Ties together the handshake (``session.py``), the message receive loop, the
video decoder (``video.py``), and input encoding (``protocol.py``/
``keymap.py``). Mirrors ``ilo3console.client``'s shape (callbacks, a
background receiver thread, a live framebuffer) even though the wire protocol
underneath is unrelated.

No power-control messages exist anywhere in the Mahogany message catalog
(unlike iLO 3's console channel, which has one) — chassis power is a
completely separate channel here: the plain HTML/CGI-style web management
UI (``power.py``), not the KVM socket.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from . import keymap, vmedia100
from . import power as pwr
from . import protocol as p
from .session import HandshakeError, KvmSession
from .video import VideoState

# Chassis power can change through means entirely outside this app's control
# (the device's own browser-based web UI, a physical button, IPMI, another
# console instance) -- a fetch-once-at-connect-and-after-our-own-actions
# approach leaves the lamp/screen-blanking permanently stale whenever that
# happens. Poll periodically so it self-corrects. Not too aggressive: this
# BMC's httpd is known to be fragile under load (see docs/PROTOCOL_ILO100.md).
_POWER_POLL_INTERVAL = 15.0


class ILo100Client:
    """A live Mahogany KVM session."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 80,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.session = KvmSession(
            host, port, username, password, use_ssl=use_ssl, timeout=timeout
        )
        self.video = VideoState()

        self._thread: threading.Thread | None = None
        self._power_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pressed_hid: set = set()
        self._has_full_control = False
        self._requested_control = False
        self._last_power_status: str | None = None
        self._storage_allowed = False
        self._storage_server: vmedia100.StorageServer | None = None

        # Callbacks (all optional).
        self.on_frame: Callable[[], None] = lambda: None
        self.on_resize: Callable[[int, int], None] = lambda w, h: None
        self.on_disconnected: Callable[[str], None] = lambda reason: None
        self.on_key_indicators: Callable[[bool, bool, bool], None] = (
            lambda c, n, s: None
        )
        self.on_control_changed: Callable[[bool], None] = lambda has_control: None
        self.on_power_status: Callable[[str], None] = lambda status: None

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> p.ServerHandshake:
        """Perform the handshake, start the receiver/power-poller threads, and
        request the current video mode + absolute mouse coordinates."""
        handshake = self.session.connect()
        self._spawn_receiver()
        self._spawn_power_poller()
        # Ask the server to (re)announce the current video mode so we get an
        # InformVesaMode promptly instead of waiting for the next natural one.
        self.session.send(p.encode_request_vesa_mode())
        # Mouse input below always sends framebuffer-pixel coordinates
        # (ButtonStateAtAbsolute), so tell the server to expect that mode.
        self.session.send(p.encode_client_absolute_mode(True))
        return handshake

    def _spawn_receiver(self) -> None:
        t = threading.Thread(target=self._run_receiver, daemon=True)
        t.start()
        self._thread = t

    def _spawn_power_poller(self) -> None:
        t = threading.Thread(target=self._run_power_poller, daemon=True)
        t.start()
        self._power_thread = t

    def _run_power_poller(self) -> None:
        # First poll fires immediately (covers the "connected via our own UI,
        # but power was already changed by something else moments earlier"
        # case); get_power_status() has its own internal retry-with-backoff
        # for this BMC's known httpd flakiness, so a single call here is
        # already resilient -- a poll that still fails just tries again next
        # interval rather than raising.
        while not self._stop.is_set():
            try:
                status = self.get_power_status()
            except pwr.PowerControlError:
                status = None
            if status is not None and status != self._last_power_status:
                self._last_power_status = status
                self.on_power_status(status)
            if self._stop.wait(_POWER_POLL_INTERVAL):
                break

    def _run_receiver(self) -> None:
        reader = self.session.reader
        # Distinguish *why* the receiver stopped -- a raw socket reset (this
        # BMC's httpd/kvm service is known to be flaky, see docs/
        # PROTOCOL_ILO100.md) and a genuine protocol desync (unknown message
        # id, decode bug) both used to surface as the same generic
        # "connection closed" with no way to tell them apart after the fact.
        reason = "connection closed"
        try:
            while not self._stop.is_set() and self.session.connected:
                msg_id = reader.read_u8()
                self._dispatch(msg_id, reader)
        except (OSError, ConnectionError) as exc:
            reason = f"connection closed: {exc}"
        except HandshakeError as exc:
            reason = f"protocol error: {exc}"
            # still crash loudly (traceback to stderr) after notifying the UI below
            raise
        finally:
            was_connected = self.session.connected
            self.session.close()
            if was_connected and not self._stop.is_set():
                self.on_disconnected(reason)

    def _dispatch(self, msg_id: int, reader) -> None:
        if msg_id == p.MSG_INFORM_VESA_MODE:
            mode = p.decode_inform_vesa_mode(reader)
            self.video.set_vesa_mode(
                mode.mode_number, mode.bpp, mode.width, mode.height
            )
            self.on_resize(self.video.width, self.video.height)
            self.on_frame()
        elif msg_id == p.MSG_BIT_BLT:
            self.video.process_bit_blt(p.decode_bit_blt(reader))
            self.on_frame()
        elif msg_id == p.MSG_ENHANCE_BIT_BLT:
            msg = p.decode_enhance_bit_blt(reader)
            if msg.blit_type in (p.BLT_ENHANCE, p.BLT_ENHANCE_NEW_FRAME):
                self.video.process_enhance_bit_blt(msg)
            elif msg.blit_type in (p.BLT_ENHANCE_HLC, p.BLT_ENHANCE_HLC_NEW_FRAME):
                self.video.process_enhance_bit_blt_hlc(msg)
            elif msg.blit_type in (p.BLT_SPECIAL_4BPP, p.BLT_SPECIAL_4BPP_NEW_FRAME):
                self.video.process_enhance_bit_blt_4bpp(msg)
            self.on_frame()
        elif msg_id == p.MSG_SET_PALETTE:
            self.video.set_palette(p.decode_set_palette(reader))
            self.on_frame()
        elif msg_id == p.MSG_SET_TEXT_CURSOR:
            self.video.set_text_cursor(p.decode_set_text_cursor(reader))
            self.on_frame()
        elif msg_id == p.MSG_SPECIAL_GRAPHICS_BIT:
            self.video.apply_special_graphics_bit(p.decode_special_graphics_bit(reader))
        elif msg_id == p.MSG_MATROX_GRAPHICS_CURSOR:
            # hardware cursor overlay: not rendered
            p.decode_matrox_graphics_cursor(reader)
        elif msg_id == p.MSG_STANDBY_POWER:
            p.decode_standby_power(reader)
        elif msg_id == p.MSG_INFORM_KEY_INDICATORS:
            caps, num, scroll = p.decode_inform_key_indicators(reader)
            self.on_key_indicators(caps, num, scroll)
        elif msg_id == p.MSG_MULTI_USER_STATE:
            self._apply_multi_user_state(p.decode_multi_user_state(reader))
        elif msg_id == p.MSG_SERVER_DISCONNECT:
            info = p.decode_server_disconnect(reader)
            self.on_disconnected(info.message or f"disconnected (reason {info.reason})")
            self._stop.set()
            self.session.close()
        elif msg_id == p.MSG_FIRMWARE_VERSION:
            p.decode_firmware_version(reader)
        elif msg_id == p.MSG_STORAGE_STATUS:
            p.decode_storage_status(reader)
        elif msg_id == p.MSG_GRAPHICS_REGISTER_VALUE:
            p.decode_graphics_register_value(reader)
        else:
            raise HandshakeError(f"unknown message id {msg_id}; stream desynced")

    def _apply_multi_user_state(self, mus: p.MultiUserState) -> None:
        # The iLO's own web console only sends RequestPrimaryControl after a
        # human clicks "OK" on a "request full control?" dialog -- a fresh
        # session otherwise sits in view-only (user_status == 1) forever:
        # the server accepts KeyStateChange /
        # ButtonStateAtAbsolute from a view-only client but silently drops
        # them. There's no dialog here, and this console has exactly one
        # user, so auto-accept it the moment we're offered control.
        self._storage_allowed = mus.allowed_to_do_storage
        had_control = self._has_full_control
        self._has_full_control = mus.has_full_control
        if mus.has_full_control:
            self._requested_control = False
        elif mus.allowed_to_take_full_control and not self._requested_control:
            self.session.send(p.encode_request_primary_control())
            self._requested_control = True
        if mus.has_full_control != had_control:
            self.on_control_changed(mus.has_full_control)

    def close(self) -> None:
        """Stop receiver/power threads, close the session, and stop any
        virtual media server."""
        self._stop.set()
        self.session.close()
        if self._storage_server is not None:
            self._storage_server.stop()
            self._storage_server = None

    def __enter__(self) -> ILo100Client:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- input --------------------------------------------------------------

    def send_key(self, browser_code: str, down: bool) -> None:
        """Translate a browser key code to a HID code and send its up/down state.

        Tracks currently-pressed HID codes so ``release_all_keys`` can clean up."""
        hid = keymap.hid_for_code(browser_code)
        if hid == -1:
            return
        if down:
            self._pressed_hid.add(hid)
        else:
            self._pressed_hid.discard(hid)
        self.session.send(p.encode_key_state_change(hid, down))

    def release_all_keys(self) -> None:
        """Send a key-up for every currently-tracked pressed HID code
        (e.g. on focus loss)."""
        for hid in list(self._pressed_hid):
            self.session.send(p.encode_key_state_change(hid, False))
        self._pressed_hid.clear()

    def send_ctrl_alt_del(self) -> None:
        """Synthesize Ctrl+Alt+Delete as individual key events, held briefly.

        There is no dedicated message for this in the Mahogany catalog
        (unlike iLO 3's console channel) — `KeyStateChange` only ever
        carries one key at a time, so this is three keys pressed in order,
        held down together, then released in reverse. The hold is required:
        firing all three down events immediately followed by all three up
        events (effectively zero hold time) is fast enough that guest
        BIOS/OS keyboard controllers don't reliably sample all three as
        simultaneously down, so the combo silently fails to register.
        """
        for hid in (keymap.HID_LCTRL, keymap.HID_LALT, keymap.HID_DELETE):
            self.session.send(p.encode_key_state_change(hid, True))
        time.sleep(0.15)
        for hid in (keymap.HID_DELETE, keymap.HID_LALT, keymap.HID_LCTRL):
            self.session.send(p.encode_key_state_change(hid, False))

    def send_mouse_absolute(
        self, x: int, y: int, left: bool, right: bool, middle: bool
    ) -> None:
        """Send a mouse move + button state at absolute framebuffer
        coordinates (x, y)."""
        self.session.send(p.encode_button_state_at_absolute(x, y, left, right, middle))

    def set_mouse_absolute_mode(self, enabled: bool = True) -> None:
        """Tell the server whether the client will send absolute (vs.
        relative) mouse coordinates."""
        self.session.send(p.encode_client_absolute_mode(enabled))

    def request_primary_control(self) -> None:
        """Ask the server to grant this client full (input) control of the session."""
        self.session.send(p.encode_request_primary_control())

    def relinquish_full_control(self) -> None:
        """Give up full control, dropping back to view-only."""
        self.session.send(p.encode_relinquish_full_control())

    def request_key_indicators(self) -> None:
        """Ask the server to report the current caps/num/scroll lock LED state."""
        self.session.send(p.encode_request_key_indicators())

    def refresh_screen(self, regions=None) -> None:
        """Ask the server to resend the given (or, by default, the whole)
        screen region."""
        self.session.send(p.encode_invalidate(regions or []))

    # -- chassis power (separate HTTP channel, not the KVM socket) ----------

    def get_power_status(self) -> str:
        """Blocking HTTP request -- call off the UI/receiver thread."""
        return pwr.get_power_status(
            self.host, self.session.username, self.session.password,
            web_port=self.port, use_ssl=self.session.use_ssl,
            timeout=self.session.timeout,
        )

    def send_power_action(self, action: str) -> str:
        """Blocking HTTP request -- call off the UI/receiver thread. Returns
        the resulting ``"ON"``/``"OFF"`` status.

        Unlike ``get_power_status``, this GET isn't idempotent, so a failure
        here does *not* retry the action itself -- this BMC's httpd is known
        to reset a connection intermittently, and the action may well have
        already landed even though we lost the confirmation. Instead, fall
        back to a (retried, read-only) status check so callers get an
        accurate lamp state instead of one stuck on the pre-action reading.
        """
        try:
            return pwr.send_power_action(
                self.host, self.session.username, self.session.password, action,
                web_port=self.port, use_ssl=self.session.use_ssl,
                timeout=self.session.timeout,
            )
        except pwr.PowerControlError:
            return self.get_power_status()

    # -- virtual media (separate data channel, not the KVM socket) ---------

    def mount_media(
        self, path: str, *, media_type: int = p.MEDIA_TYPE_CD_ISO_IMAGE
    ) -> None:
        """Serve ``path`` to the iLO as virtual media.

        Requires full control (``self._has_full_control``) *and*
        ``self._storage_allowed`` (``MultiUserState.allowed_to_do_storage``)
        -- the iLO's own web console gates ``StorageClientConnect`` on this
        same flag, which ``MultiUserState`` updates keep overwriting.
        Observed live staying false even with full control and an "LO100
        Advanced" license installed; what actually flips it true is not yet
        confirmed (see docs/PROTOCOL_ILO100.md) -- this method will
        silently be ignored by the server if it's still false, exactly
        like the reference client's own gating.
        """
        if self._storage_server is not None:
            self.unmount_media()
        media = vmedia100.FileMedia(path)
        my_ip = vmedia100.local_ip_toward(self.host, self.port)
        srv = vmedia100.StorageServer(media, port=0)
        srv.start()
        self._storage_server = srv
        ip_bytes = socket.inet_aton(my_ip)
        self.session.send(
            p.encode_storage_client_connect(ip_bytes, srv.port, 0, media_type, path)
        )

    def unmount_media(self) -> None:
        """Tell the server to disconnect virtual media and stop the local
        storage server."""
        self.session.send(p.encode_storage_client_disconnect())
        if self._storage_server is not None:
            self._storage_server.stop()
            self._storage_server = None
