"""Native desktop app for the iLO 100 console — connects directly, no bridge.

Mirrors ``ilo3console.desktop``: Python talks to the iLO directly (HTTP
Digest login -> httpdata token -> Mahogany socket handshake, all in
``client.py``) and pushes frames straight into a pywebview window via
``window.evaluate_js``. No loopback server, no open port, no ``websockets``
dependency on this path.

Run::

    python -m ilo100console.desktop
    python -m ilo100console.desktop --host ilo.example --user Administrator
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import threading

from .client import ILo100Client

# Cap on how often a frame is actually rendered+pushed to the window. Text-mode
# echo alone can fire on_frame() several times per keystroke (character BitBlt
# + cursor move, sometimes a palette refresh too); render_rgb() + PNG-encode a
# 640x400 framebuffer in pure Python costs ~25ms, and doing that synchronously
# once per *message* (see below) rather than once per *screen update* made
# typing visibly lag more and more the faster you typed.
_MIN_FRAME_INTERVAL = 1.0 / 30

try:
    import webview
except ImportError:  # pragma: no cover - optional dependency
    webview = None

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _apply_input(client: ILo100Client, msg: dict) -> None:
    kind = msg.get("t")
    if kind == "key":
        client.send_key(msg.get("code", ""), bool(msg.get("down")))
    elif kind == "keyup":
        client.release_all_keys()
    elif kind == "mouse":
        client.send_mouse_absolute(
            msg["x"], msg["y"],
            bool(msg.get("left")), bool(msg.get("right")), bool(msg.get("middle")),
        )
    elif kind == "cad":
        client.send_ctrl_alt_del()
    elif kind == "refresh":
        client.refresh_screen()


class Api:
    """Exposed to the webview's JavaScript as ``window.pywebview.api``.

    Every stored object is ``_``-prefixed on purpose: pywebview's bridge
    walks *public* attributes with ``dir()`` and recurses into any object it
    finds, which would otherwise introspect the native window and the whole
    client graph — producing "unable to get the value" log spam. See
    ilo3console.desktop for the same fix, discovered there first.
    """

    def __init__(self, prefill: dict) -> None:
        self._prefill = prefill
        self._window = None
        self._client: ILo100Client | None = None
        self._frame_dirty = threading.Event()
        self._pusher_stop = threading.Event()
        self._pusher_thread: threading.Thread | None = None

    def get_prefill(self) -> dict:
        """Return the host/user values to pre-populate the connect dialog with."""
        return self._prefill

    def connect(self, params: dict) -> dict:
        """Open the iLO session.

        Returns ``{ok, host, privileges}`` or ``{ok:false, error}``.
        """
        try:
            client = ILo100Client(
                params["host"],
                params["username"],
                params["password"],
                port=int(params.get("port") or 80),
                use_ssl=bool(params.get("ssl")),
            )
            self._wire(client)
            handshake = client.connect()
            self._client = client
            self._pusher_stop.clear()
            self._pusher_thread = threading.Thread(target=self._run_pusher, daemon=True)
            self._pusher_thread.start()
            return {
                "ok": True,
                "host": params["host"],
                "privileges": {
                    "keyboard": handshake.keyboard,
                    "video": handshake.video,
                    "mouse": handshake.mouse,
                },
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def power(self, action: str) -> dict:
        """Blocking (runs on the JS-bridge call thread, not the GTK main
        loop / video pusher): submits a chassis power action over HTTP and
        returns ``{ok, status}`` or ``{ok: false, error}``."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            status = self._client.send_power_action(action)
            return {"ok": True, "status": status}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    # -- virtual media --------------------------------------------------
    #
    # Local-file mounting is native-only: it's Python opening a file on
    # *this* machine and serving it to the iLO over the storage channel,
    # which only makes sense when Python and the picked file are on the
    # same box (true here, not true for the browser bridge).
    #
    # Experimental: the server also gates the storage channel on a
    # permission flag (MultiUserState.isAllowedToDoStorage) that hasn't
    # reliably flipped true in live testing so far -- see
    # docs/PROTOCOL_ILO100.md's "Virtual media" section.

    def vm_pick_file(self) -> dict:
        """Open a native file-open dialog. Returns ``{ok, path}`` or ``{ok:false}``."""
        if self._window is None:
            return {"ok": False, "error": "no window"}
        try:
            result = self._window.create_file_dialog(webview.OPEN_DIALOG)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}
        if not result:
            return {"ok": False, "error": None}  # user cancelled, not an error
        return {"ok": True, "path": result[0]}

    def vm_mount(self, path: str, media_type: int) -> dict:
        """Mount the local file at ``path`` as virtual media of the given type."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            self._client.mount_media(path, media_type=int(media_type))
            return {"ok": True}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def vm_unmount(self) -> dict:
        """Unmount whatever virtual media is currently attached."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            self._client.unmount_media()
            return {"ok": True}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def input(self, msg_json: str) -> None:
        """Decode a JSON input event from the JS bridge and apply it to the client.

        Called directly from the webview's JS bridge on every keystroke and
        mouse move: an uncaught exception here surfaces as a raw pywebview
        traceback with no way for the UI to react, so stay defensive.
        """
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            _apply_input(self._client, json.loads(msg_json))

    def disconnect(self) -> None:
        """Alias for ``close()``, exposed to the JS bridge as the disconnect action."""
        self.close()

    def _wire(self, client: ILo100Client) -> None:
        # on_frame fires synchronously on the receiver thread for every single
        # protocol message -- just flip a flag here (cheap, never blocks the
        # socket read loop) and let the pusher thread coalesce bursts into one
        # render at a capped rate instead of rendering per message.
        client.on_frame = self._frame_dirty.set
        client.on_resize = lambda w, h: self._event(t="resize", w=w, h=h)
        client.on_disconnected = lambda reason: self._event(
            t="disconnected", reason=reason
        )
        client.on_key_indicators = lambda c, n, s: self._event(
            t="key_indicators", caps=c, num=n, scroll=s
        )
        client.on_control_changed = lambda has_control: self._event(
            t="control", has_control=has_control
        )
        client.on_power_status = lambda status: self._event(t="power", status=status)

    def _run_pusher(self) -> None:
        while not self._pusher_stop.is_set():
            if self._frame_dirty.wait(timeout=0.5):
                self._frame_dirty.clear()
                self._push_frame()
                self._pusher_stop.wait(timeout=_MIN_FRAME_INTERVAL)

    def _push_frame(self) -> None:
        if self._client is None:
            return
        v = self._client.video
        if v.width == 0 or v.height == 0:
            return
        from ilo3console.png import encode_png  # pylint:disable=import-outside-toplevel

        png = encode_png(v.width, v.height, v.render_rgb())
        b64 = base64.b64encode(png).decode("ascii")
        self.eval_js(f"window.__onFrame('{b64}')")

    def _event(self, **payload) -> None:
        self.eval_js(f"window.__onEvent({json.dumps(json.dumps(payload))})")

    def eval_js(self, js: str) -> None:
        """Run ``js`` in the webview window, swallowing errors if it's gone."""
        if self._window is None:
            return
        # window may be closing / not yet loaded
        with contextlib.suppress(Exception):
            self._window.evaluate_js(js)

    def close(self) -> None:
        """Stop the frame-pusher thread and close the iLO client connection."""
        self._pusher_stop.set()
        if self._client is not None:
            self._client.close()
            self._client = None


def main() -> None:
    """Parse CLI args and run the native pywebview iLO 100 console app."""
    parser = argparse.ArgumentParser(description="Native iLO 100 console (webview)")
    parser.add_argument("--host", help="pre-fill the iLO host in the dialog")
    parser.add_argument("--user", help="pre-fill the username in the dialog")
    args = parser.parse_args()

    if webview is None:
        raise SystemExit(
            "pywebview is not installed. Run: pip install pywebview\n"
            "(Linux also needs a WebKitGTK or Qt WebEngine backend.)"
        )

    prefill = {}
    if args.host:
        prefill["host"] = args.host
    if args.user:
        prefill["user"] = args.user
    # Set by launcher/desktop.py when it has an OS-keyring-saved password for
    # this connection; popped immediately so it doesn't linger in our own
    # environ any longer than it has to.
    password = os.environ.pop("ILO_CONSOLE_PASSWORD", None)
    if password:
        prefill["password"] = password

    api = Api(prefill)
    window = webview.create_window(
        "iLO 100 Remote Console",
        os.path.join(_WEB_DIR, "index.html"),
        js_api=api,
        width=960,
        height=760,
        min_size=(720, 560),
        background_color="#0b0e10",
    )
    # private on purpose; keeps the native window off the JS bridge
    api._window = window  # pylint: disable=protected-access

    def _on_closed() -> None:
        # See ilo3console.desktop for why os._exit is used here: webview.
        # start() is supposed to return once every window closes, but some
        # backends (GTK) can leave the process hanging on a lingering
        # native event loop.
        api.close()
        os._exit(0)

    window.events.closed += _on_closed
    try:
        webview.start()
    finally:
        api.close()


if __name__ == "__main__":
    main()
