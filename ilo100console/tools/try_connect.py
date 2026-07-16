"""Standalone connection smoke test for the iLO 100 / Mahogany client.

Connects, completes the handshake, and reports the first few seconds of
server messages plus a PNG dump of the first frame — for validating the
protocol implementation (see docs/PROTOCOL_ILO100.md) against a real device.

Usage::

    python -m ilo100console.tools.try_connect --host 10.0.0.5 --user Administrator
    # password is prompted; --ssl for the kvmssl session type; --port to override

Writes ``/tmp/ilo100_frame.png`` with the first rendered frame, if any.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("/ilo100console/", 1)[0])

from ilo3console.png import (  # noqa: E402  pylint: disable=wrong-import-position
    encode_png,  # shared dependency-free PNG encoder
)
from ilo100console.client import (  # noqa: E402  pylint: disable=wrong-import-position
    ILo100Client,
)


def main() -> None:
    """Connect to a live iLO 100, log server events for a few seconds,
    and dump the first frame to PNG."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--ssl", action="store_true")
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()

    password = os.environ.get("ILO100_PASS") or getpass.getpass(
        f"Password for {args.user}@{args.host}: "
    )

    client = ILo100Client(
        args.host, args.user, password, port=args.port, use_ssl=args.ssl
    )
    events = []
    client.on_frame = lambda: events.append(
        ("frame", client.video.width, client.video.height)
    )
    client.on_resize = lambda w, h: events.append(("resize", w, h))
    client.on_disconnected = lambda reason: events.append(("disconnected", reason))
    client.on_key_indicators = lambda c, n, s: events.append(
        ("key_indicators", c, n, s)
    )

    print(f"Connecting to {args.host}:{args.port} (ssl={args.ssl}) as {args.user!r}...")
    try:
        handshake = client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"CONNECT FAILED: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc

    print(f"ServerHandshake OK: {handshake}")
    print(f"Watching for {args.seconds}s...")
    deadline = time.monotonic() + args.seconds
    last_report = 0
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if len(events) != last_report:
            for e in events[last_report:]:
                print(" event:", e)
            last_report = len(events)

    print(f"Total events: {len(events)}")
    v = client.video
    print(f"Video: mode={v.mode} {v.width}x{v.height} bpp={v.bpp}")

    if client.video.width and client.video.height:
        rgb = client.video.render_rgb()
        png = encode_png(client.video.width, client.video.height, rgb)
        out_path = "/tmp/ilo100_frame.png"
        with open(out_path, "wb") as fh:
            fh.write(png)
        print(f"Wrote {out_path}")
    else:
        print("No video frame received yet.")

    client.close()


if __name__ == "__main__":
    main()
