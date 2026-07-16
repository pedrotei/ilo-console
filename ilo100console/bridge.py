"""WebSocket bridge exposing an iLO 100 console session to a browser front-end.

Mirrors ``ilo3console.bridge``. A browser can't open a raw TCP socket or do
the Digest-auth + Mahogany-handshake dance itself, so this bridge does it on
the browser's behalf and streams frames over WebSocket as PNGs.

Run::

    python -m ilo100console.bridge --listen 127.0.0.1:8081
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

from .client import ILo100Client

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _snapshot_png(client: ILo100Client) -> bytes | None:
    from ilo3console.png import encode_png  # pylint:disable=import-outside-toplevel

    v = client.video
    if v.width == 0 or v.height == 0:
        return None
    return encode_png(v.width, v.height, v.render_rgb())


class _Session:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.client: ILo100Client | None = None
        self._frame_ready = asyncio.Event()

    async def run(self) -> None:
        """Drive one browser WebSocket connection end to end: wait for the
        initial connect message, open the iLO client, then pump frames and
        input until closed."""
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
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            await self._send_json(t="error", msg=str(exc))
        finally:
            if self.client:
                await asyncio.to_thread(self.client.close)

    async def _connect_ilo(self, params: dict) -> None:
        client = ILo100Client(
            params["host"],
            params["username"],
            params["password"],
            port=int(params.get("port", 80)),
            use_ssl=bool(params.get("ssl")),
        )
        client.on_frame = lambda: self.loop.call_soon_threadsafe(self._frame_ready.set)
        client.on_resize = lambda w, h: self._emit(t="resize", w=w, h=h)
        client.on_disconnected = lambda reason: self._emit(
            t="disconnected", reason=reason
        )
        client.on_key_indicators = lambda c, n, s: self._emit(
            t="key_indicators", caps=c, num=n, scroll=s
        )
        client.on_control_changed = lambda has_control: self._emit(
            t="control", has_control=has_control
        )
        # client.connect() spawns its own periodic power-status poller (fires
        # immediately, then every _POWER_POLL_INTERVAL) -- no separate fetch
        # needed here.
        client.on_power_status = lambda status: self._emit(t="power", status=status)
        self.client = client
        handshake = await asyncio.to_thread(client.connect)
        await self._send_json(
            t="ready",
            host=params["host"],
            privileges={
                "keyboard": handshake.keyboard,
                "video": handshake.video,
                "mouse": handshake.mouse,
            },
        )

    async def _sender(self) -> None:
        while True:
            await self._frame_ready.wait()
            self._frame_ready.clear()
            png = await asyncio.to_thread(_snapshot_png, self.client)
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
            await self._handle_input(msg)

    async def _handle_input(self, msg: dict) -> None:
        client = self.client
        if client is None:
            return
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
            # send_ctrl_alt_del() holds the keys down for ~150ms (time.sleep)
            # -- run it off the event loop so it doesn't stall every other
            # connection's frame pump/input for that window.
            await asyncio.to_thread(client.send_ctrl_alt_del)
        elif kind == "refresh":
            client.refresh_screen()
        elif kind == "power":
            try:
                status = await asyncio.to_thread(
                    client.send_power_action, msg.get("action", "")
                )
                await self._send_json(t="power", status=status)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                await self._send_json(t="power_error", msg=str(exc))

    def _emit(self, **payload) -> None:
        self.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._send_json(**payload))
        )

    async def _send_json(self, **payload) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps(payload))


async def _handler(ws: ServerConnection) -> None:
    await _Session(ws).run()


def _serve_static(connection: ServerConnection, request) -> Response | None:
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
        return Response(HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"missing\n")
    headers = Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    return Response(HTTPStatus.OK, "OK", headers, body)


async def run_bridge(host: str, port: int) -> None:
    """Start the WebSocket server (serving static assets and the console protocol)
    and run until cancelled."""
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    async with serve(_handler, host, port, process_request=_serve_static):
        print(f"iLO 100 console bridge on http://{host}:{port}/")
        await asyncio.Future()


def main() -> None:
    """Parse CLI args and run the WebSocket bridge server."""
    parser = argparse.ArgumentParser(description="iLO 100 console WebSocket bridge")
    parser.add_argument(
        "--listen", default="127.0.0.1:8081", help="host:port to listen on"
    )
    args = parser.parse_args()
    host, _, port = args.listen.rpartition(":")
    asyncio.run(run_bridge(host or "127.0.0.1", int(port)))


if __name__ == "__main__":
    main()
