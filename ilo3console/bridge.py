"""WebSocket bridge exposing an iLO 3 console session to a browser front-end.

Serves the static front-end (`web/index.html`) and a WebSocket endpoint on the
same port. When a browser connects it sends a JSON ``connect`` message with the
iLO host and credentials; the bridge opens an :class:`ILoConsoleClient`, streams
each decoded video frame to the browser as a binary PNG, forwards status/power
events as JSON, and relays keyboard/mouse/power input back to the iLO.

Run::

    python -m ilo3console.bridge --listen 127.0.0.1:8080

Then open http://127.0.0.1:8080/ and enter the iLO host + credentials.

The bridge terminates its own (browser-facing) TLS-less HTTP; put it behind a
reverse proxy for anything beyond localhost. Credentials are supplied per browser
session and never stored.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from http import HTTPStatus

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from . import input as inp
from .client import ILoConsoleClient
from .png import snapshot_png
from .session import AuthError

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class _Session:
    """One browser connection bridged to one iLO console client."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.client: ILoConsoleClient | None = None
        self._frame_ready = asyncio.Event()

    async def run(self) -> None:
        """Handle one browser WebSocket connection end-to-end.

        Waits for the initial ``connect`` message, opens the iLO client, then
        runs the frame-sender and input loop concurrently until the socket
        closes or an error occurs; always tears down the client on exit.
        """
        try:
            first = await self.ws.recv()
            params = json.loads(first)
            if params.get("t") != "connect":
                await self._send_json(t="error", msg="expected connect message")
                return
            await self._connect_ilo(params)
            sender = asyncio.create_task(self._sender())
            try:
                await self._input_loop()
            finally:
                sender.cancel()
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except AuthError as exc:
            await self._send_json(t="error", msg=str(exc))
        finally:
            if self.client:
                await asyncio.to_thread(self.client.close)

    async def _connect_ilo(self, params: dict) -> None:
        client = ILoConsoleClient(
            params["host"],
            username=params.get("username"),
            password=params.get("password"),
            session_key=params.get("token"),
            port=int(params.get("port", 443)),
        )
        # Wire callbacks (fired from client receiver threads → hop to the loop).
        client.on_frame = lambda: self.loop.call_soon_threadsafe(
            self._frame_ready.set
        )
        client.on_power = lambda on: self._emit(t="power", on=on)
        client.on_health = lambda level: self._emit(t="health", level=level)
        client.on_status = lambda field, text: self._emit(
            t="status", field=field, text=text
        )
        client.on_text = lambda text: self._emit(t="text", text=text)
        client.on_resize = lambda w, h: self._emit(t="resize", w=w, h=h)
        client.on_seize = lambda: self._emit(t="seize")
        client.on_cipher = lambda cipher: self._emit(t="cipher", cipher=cipher)
        client.on_disconnected = lambda: self._emit(t="disconnected")
        self.client = client
        await asyncio.to_thread(client.connect)
        await self._send_json(t="ready", host=params["host"])

    async def _sender(self) -> None:
        while True:
            await self._frame_ready.wait()
            self._frame_ready.clear()
            png = await asyncio.to_thread(snapshot_png, self.client.framebuffer)
            if png is not None:
                await self.ws.send(png)

    async def _input_loop(self) -> None:
        async for message in self.ws:
            if isinstance(message, bytes):
                continue
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                continue
            if msg.get("t") in ("vm_insert_url", "vm_eject_url", "vm_status"):
                await self._handle_vm(msg)  # does blocking HTTP; keep off the loop
            else:
                self._handle_input(msg)

    async def _handle_vm(self, msg: dict) -> None:
        # URL-based virtual media only: the iLO fetches the image itself, so
        # unlike ilo3console.desktop there's no local file to hand it — no
        # SCSI-target channel here, just the JSON call, offloaded to a thread
        # since it's a blocking HTTP request.
        client = self.client
        if client is None:
            return
        kind = msg.get("t")
        try:
            if kind == "vm_insert_url":
                result = await asyncio.to_thread(
                    client.insert_url_media, msg["device"], msg["url"]
                )
                await self._send_json(
                    t="vm_result", device=msg["device"], result=result
                )
            elif kind == "vm_eject_url":
                result = await asyncio.to_thread(client.eject_url_media, msg["device"])
                await self._send_json(
                    t="vm_result", device=msg["device"], result=result
                )
            elif kind == "vm_status":
                status = await asyncio.to_thread(client.vm_status)
                await self._send_json(t="vm_status", status=status)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            await self._send_json(
                t="vm_result", device=msg.get("device"), result=str(exc)
            )

    def _handle_input(self, msg: dict) -> None:
        client = self.client
        if client is None:
            return
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
            client.send_power(int(msg.get("option", inp.POWER_PULSE)))
        elif kind == "cad":
            client.send_ctrl_alt_del()
        elif kind == "refresh":
            client.refresh_screen()

    def _emit(self, **payload) -> None:
        """Schedule a JSON message from a non-async (client thread) context."""
        self.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._send_json(**payload))
        )

    async def _send_json(self, **payload) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps(payload))


async def _handler(ws: ServerConnection) -> None:
    await _Session(ws).run()


def _serve_static(connection: ServerConnection, request) -> Response | None:
    """Serve the front-end for plain HTTP GETs; return None to allow WS upgrade."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?", 1)[0]
    if path in ("/", "/index.html"):
        return _file_response("index.html", "text/html; charset=utf-8")
    if path == "/app.js":
        return _file_response("app.js", "text/javascript; charset=utf-8")
    return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")


def _file_response(name: str, content_type: str) -> Response:
    try:
        with open(os.path.join(_WEB_DIR, name), "rb") as fh:
            body = fh.read()
    except OSError:
        return Response(
            HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"missing\n"
        )
    headers = Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    return Response(HTTPStatus.OK, "OK", headers, body)


async def run_bridge(host: str, port: int) -> None:
    """Start the WebSocket/HTTP server on ``host``:``port`` and serve forever."""
    # Per-connection handshake failures (health probes, port scanners, aborted
    # loads) are noise here, not actionable — keep them out of the console.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    async with serve(_handler, host, port, process_request=_serve_static):
        print(f"iLO console bridge on http://{host}:{port}/")
        await asyncio.Future()  # run forever


def main() -> None:
    """Parse the ``--listen`` CLI arg and run the bridge server."""
    parser = argparse.ArgumentParser(description="iLO 3 console WebSocket bridge")
    parser.add_argument(
        "--listen", default="127.0.0.1:8080", help="host:port to listen on"
    )
    args = parser.parse_args()
    host, _, port = args.listen.rpartition(":")
    asyncio.run(run_bridge(host or "127.0.0.1", int(port)))


if __name__ == "__main__":
    main()
