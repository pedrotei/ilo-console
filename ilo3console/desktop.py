"""Native desktop app — the full UI in an OS-native window, connecting directly.

Unlike the browser (which can't open raw TCP or speak iLO's legacy-TLS console
protocol, and so needs the WebSocket bridge), the desktop app talks to the iLO
**directly in Python** and pushes frames straight into the webview via
pywebview's native JS API. No loopback server, no open port, no ``websockets``.

The same `web/` front-end runs inside the webview; it detects the native API and
uses it instead of a WebSocket.

Run::

    python -m ilo3console.desktop                 # opens the connect dialog
    python -m ilo3console.desktop --host ilo.example --user Administrator

``--host`` / ``--user`` merely pre-fill the dialog; the password is typed in the
window. Requires ``pywebview`` (``pip install pywebview``) and its platform
backend (bundled on macOS/Windows; Linux needs WebKitGTK or Qt WebEngine).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import threading

from .client import ILoConsoleClient
from .png import snapshot_png

try:
    import webview
except ImportError:  # pragma: no cover - optional dependency
    webview = None

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def _apply_input(client: ILoConsoleClient, msg: dict) -> None:
    kind = msg.get("t")
    if kind == "key":
        client.send_key_report(msg.get("mod", 0), msg.get("keys", []))
    elif kind == "keyup":
        client.release_keys()
    elif kind == "mouse":
        client.send_mouse(
            msg["x"], msg["y"], msg.get("buttons", 0),
            msg.get("dx", 0), msg.get("dy", 0),
        )
    elif kind == "power":
        client.send_power(int(msg.get("option", 0)))
    elif kind == "cad":
        client.send_ctrl_alt_del()
    elif kind == "refresh":
        client.refresh_screen()


class _FramePusher(threading.Thread):
    """Coalesces frame-ready signals and ships PNGs to the webview off-thread."""

    def __init__(self, api: Api) -> None:
        super().__init__(daemon=True)
        self._api = api
        self._ready = threading.Event()
        self._stop = False

    def notify(self) -> None:
        """Signal that a new frame is available for the run loop to push."""
        self._ready.set()

    def stop(self) -> None:
        """Ask the run loop to exit and wake it if it's waiting."""
        self._stop = True
        self._ready.set()

    def run(self) -> None:
        while not self._stop:
            self._ready.wait()
            self._ready.clear()
            client = self._api._client  # pylint: disable=protected-access
            if self._stop or client is None:
                continue
            png = snapshot_png(client.framebuffer)
            if png is not None:
                b64 = base64.b64encode(png).decode("ascii")
                self._api.eval_js(f"window.__onFrame('{b64}')")


class Api:
    """Exposed to the webview's JavaScript as ``window.pywebview.api``.

    Only the public methods below (``get_prefill``, ``connect``, ``input``,
    ``disconnect``) should reach JS. Every stored object is ``_``-prefixed on
    purpose: pywebview's bridge walks *public* attributes with ``dir()`` and
    recurses into any object, which would otherwise introspect the native window
    (GObject) and the whole client graph — producing "unable to get the value"
    log spam and a needlessly wide exposed surface.
    """

    def __init__(self, prefill: dict) -> None:
        self._prefill = prefill
        self._window = None
        self._client: ILoConsoleClient | None = None
        self._pusher: _FramePusher | None = None

    # -- called from JS -------------------------------------------------------

    def get_prefill(self) -> dict:
        """Return the host/user values to pre-fill in the connect dialog."""
        return self._prefill

    def connect(self, params: dict) -> dict:
        """Open the iLO session. Returns ``{ok, host}`` or ``{ok:false, error}``."""
        try:
            client = ILoConsoleClient(
                params["host"],
                username=params.get("username") or None,
                password=params.get("password") or None,
                session_key=params.get("token") or None,
                port=int(params.get("port") or 443),
            )
            self._pusher = _FramePusher(self)
            self._pusher.start()
            self._wire(client)
            client.connect()
            self._client = client
            return {"ok": True, "host": params["host"]}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def input(self, msg_json: str) -> None:
        """Decode a JSON input event from JS and apply it to the client,
        swallowing errors.
        """
        # This is called directly from the webview's JS bridge on every
        # keystroke/mouse move: an uncaught exception here surfaces as a raw
        # pywebview traceback in the console with no way for the UI to react.
        # ConsoleChannel.send() already no-ops once the link is gone, but this
        # boundary stays defensive against anything else going wrong.
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            _apply_input(self._client, json.loads(msg_json))

    def disconnect(self) -> None:
        """Tear down the client and frame pusher (alias for ``close``,
        called from JS).
        """
        self.close()

    # -- virtual media ---------------------------------------------------------
    #
    # Local-file mounting is native-only: it's Python opening a file on *this*
    # machine and serving it to the iLO over the SCSI-target channel, which only
    # makes sense when Python and the picked file are on the same box (true for
    # the desktop app, not generally true for the browser bridge). URL media
    # (the iLO fetching the image itself) has no such constraint.

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

    def vm_mount(self, path: str, kind: str) -> dict:
        """Mount a local file at ``path`` as virtual media of type ``kind``
        (e.g. floppy/cdrom).
        """
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            self._client.mount_local_image(path, kind)
            return {"ok": True}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def vm_unmount(self, slot: str) -> dict:
        """Unmount the locally-mounted virtual media image in the given ``slot``."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            self._client.unmount_local_image(slot)
            return {"ok": True}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def vm_insert_url(self, device: str, url: str) -> dict:
        """Ask the iLO to fetch and insert media from ``url`` into ``device``."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            result = self._client.insert_url_media(device, url)
            return {"ok": result == "Success", "result": result}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    def vm_eject_url(self, device: str) -> dict:
        """Ask the iLO to eject the URL-based media currently attached to ``device``."""
        if self._client is None:
            return {"ok": False, "error": "not connected"}
        try:
            result = self._client.eject_url_media(device)
            return {"ok": True, "result": result}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"ok": False, "error": str(exc)}

    # -- wiring ---------------------------------------------------------------

    def _wire(self, client: ILoConsoleClient) -> None:
        client.on_frame = lambda: self._pusher and self._pusher.notify()
        client.on_power = lambda on: self._event(t="power", on=on)
        client.on_health = lambda level: self._event(t="health", level=level)
        client.on_status = lambda field, text: self._event(
            t="status", field=field, text=text
        )
        client.on_text = lambda text: self._event(t="text", text=text)
        client.on_resize = lambda w, h: self._event(t="resize", w=w, h=h)
        client.on_seize = lambda: self._event(t="seize")
        client.on_cipher = lambda cipher: self._event(t="cipher", cipher=cipher)
        client.on_disconnected = lambda: self._event(t="disconnected")

    def _event(self, **payload) -> None:
        self.eval_js(f"window.__onEvent({json.dumps(json.dumps(payload))})")

    def eval_js(self, js: str) -> None:
        """Run ``js`` in the webview window, ignoring errors if it's
        closing/not ready.
        """
        if self._window is None:
            return
        # window may be closing / not yet loaded
        with contextlib.suppress(Exception):
            self._window.evaluate_js(js)

    def close(self) -> None:
        """Stop the frame pusher thread and close the iLO client session, if open."""
        if self._pusher is not None:
            self._pusher.stop()
            self._pusher = None
        if self._client is not None:
            self._client.close()
            self._client = None


def main() -> None:
    """Parse CLI args and open the native webview window running the console UI."""
    parser = argparse.ArgumentParser(description="Native iLO 3 console (webview)")
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
    # Sized so a native 1024x768 console fits without scrollbars (canvas + the
    # header rail + footer + margins), with headroom for window chrome.
    window = webview.create_window(
        "iLO 3 Remote Console",
        os.path.join(_WEB_DIR, "index.html"),
        js_api=api,
        width=1120,
        height=940,
        min_size=(960, 700),
        background_color="#0b0e10",
    )
    # private on purpose; keeps the native window off the JS bridge
    api._window = window  # pylint: disable=protected-access

    def _on_closed() -> None:
        # Tear down the iLO session (console/cmd/vmedia sockets, all daemon
        # threads) as soon as the window closes, then force the interpreter to
        # exit. webview.start() is supposed to return once every window is
        # closed, but some backends (GTK in particular) can leave the process
        # hanging on a lingering native event loop — os._exit sidesteps that.
        api.close()
        os._exit(0)

    window.events.closed += _on_closed
    try:
        webview.start()
    finally:
        api.close()


if __name__ == "__main__":
    main()
