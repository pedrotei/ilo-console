# ilo-console

A modern, self-contained remote console for **HPE iLO**, implemented in pure
Python. Runs as a **native desktop app** or in a browser. Two controller
generations are supported, each with its own package and protocol:

| | iLO 3 | iLO 100 |
|--|--|--|
| Package | `ilo3console/` | `ilo100console/` |
| Protocol doc | [docs/PROTOCOL_ILO3.md](docs/PROTOCOL_ILO3.md) | [docs/PROTOCOL_ILO100.md](docs/PROTOCOL_ILO100.md) |
| Hardware | ProLiant DL380 G7-class (firmware 1.94) | ProLiant ML/DL 1xx-class |
| Video | Proprietary DVC codec | VNC-derived (raw + compressed BitBlt, text mode) |
| Stream cipher | In-protocol RC4/AES over the console channel | None — SSL wraps the whole socket in `kvmssl` mode |
| Virtual media | SCSI-target socket to `rc_info.vm_port` | iLO dials back to a local listener (`vmedia100.py`) |
| Power control | Command channel + input frames | Separate HTTP/CGI page (`chassis.html`) |

## Why Python

The original consoles for both of these controllers depended on a
browser-embedded Java client — long dead in every modern browser — and
tied the console to whatever OS/architecture that client happened to run
on. Reimplementing the wire protocols directly in Python removes both
constraints:

- **No runtime to install or trust.** No JVM, no browser plugin, no
  bundled Electron/Chromium. `ilo3console`/`ilo100console` need only the
  standard library plus a couple of small, pure-Python(ish) packages
  (`cryptography`, `pywebview`, `websockets`) — see [Setup](#setup).
- **Runs anywhere CPython does.** The same code path drives the native
  desktop window and the browser bridge on Linux, macOS, and Windows,
  x86-64 or ARM, without per-platform builds. `pywebview` abstracts the
  native window (WebKitGTK / WebView2 / WKWebView) so the UI itself
  (`web/`) is just HTML/CSS/JS shared across all three.
- **Headless-friendly by construction.** Because the protocol logic
  (`client.py` in each package) is fully decoupled from any UI, it's
  usable directly from a script, a test suite, a cron job, or a CI runner
  with no display and no browser at all — see
  [Use a client directly (headless)](#use-a-client-directly-headless).
- **One codebase for both controller generations.** iLO 3's DVC/telnet
  design and iLO 100's VNC-derived Mahogany protocol are structurally very
  different (see the comparison table above), but expressing both in the
  same language and process model made it straightforward to share
  utilities (`ilo100console.vmedia100` reuses `ilo3console.vmedia`'s SCSI
  command handling almost verbatim) and to keep the two desktop/bridge
  apps consistent for anyone maintaining or extending either one.

None of this requires anything exotic from Python — sockets, `ssl`,
`struct`, and a bit of bit-twiddling for the DVC codec are enough to speak
both protocols natively.

## License

[MIT](LICENSE).

## Disclaimer

This project is an independent, unofficial implementation and is provided
strictly for personal, non-commercial use with hardware the user already owns
(e.g. home-lab equipment). It is **not affiliated with, endorsed by,
sponsored by, or in any way officially connected with Hewlett Packard
Enterprise ("HPE") or any of its subsidiaries or affiliates.** "HPE,"
"ProLiant," and "iLO" (Integrated Lights-Out) are trademarks or registered
trademarks of Hewlett Packard Enterprise; any reference to them here is
purely nominative, for the sole purpose of identifying the hardware and
protocols this software is designed to interoperate with.

The iLO 3 and iLO 100 controller generations targeted by this project are
long since **end-of-life and out of support from HPE**; HPE has not sold
servers equipped with either for many years and does not provide firmware
updates, technical support, or licensing services for them as of this
writing. This project exists to keep such otherwise-unsupported, owner-owned
hardware usable — chiefly for hobbyist and home-lab purposes — after
vendor support and tooling have lapsed. It is not a substitute for, and
makes no claim to replace, any currently-supported HPE product, service, or
license (including any "Advanced" licensed feature set), and nothing in
this project should be construed as unlocking, circumventing, or granting
entitlement to any HPE license the user has not independently and lawfully
obtained.

This software is provided "AS IS," without warranty of any kind, express or
implied. Use is entirely at your own risk and, where applicable, subject to
the license terms accompanying your own hardware and any agreements you have
with HPE. Nothing herein is legal advice.

> **Note on how the protocols were understood:** HPE does not publish a
> wire-level specification for either the iLO 3 remote-console protocol or
> the iLO 100 "Mahogany" KVM protocol — its public documentation covers
> how to *use* the remote console feature, not the byte-level format
> underneath it. Understanding both protocols well enough to reimplement
> them (see docs/PROTOCOL_ILO3.md and docs/PROTOCOL_ILO100.md) involved
> AI-assisted analysis alongside live testing against real hardware. Treat
> the protocol docs accordingly: they describe this project's own
> understanding and implementation, not an official or authoritative HPE
> specification, and may contain errors despite the live-testing notes
> throughout.

## What it does

- Logs in to the iLO with a **username and password** — the client mints
  (iLO 3) or fetches (iLO 100) the session token itself; no separate login
  step is needed. iLO 3's `ILoConsoleClient` also accepts a pre-existing
  session token in place of credentials when used directly from Python (the
  desktop/browser UI forms only ever collect username + password).
- Speaks each controller's native console wire protocol directly — no browser
  plugins, no bundled runtime.
- Decodes video into a framebuffer and relays keyboard / mouse / power input.
- Exposes it all to the browser over a WebSocket bridge that streams frames as
  PNG and forwards input.

## iLO 3 (`ilo3console/`)

- `crypto.py` — RC4 + AES-OFB keystream ciphers
- `session.py` — HTTPS login + `/json/*` layer (legacy TLS)
- `console.py` — console channel: handshake + encrypted byte pump
- `dvc.py` — DVC video decoder → `Framebuffer`
- `cmdchannel.py` — command channel notifications
- `input.py` — keyboard / mouse / power frame encoders
- `vmedia.py` — local-file virtual media (client acts as a SCSI target)
- `client.py` — orchestrates a full session
- `desktop.py` + `web/` — native desktop app, connects directly
- `bridge.py` + `png.py` — WebSocket bridge for the in-browser UI

```bash
.venv/bin/python -m ilo3console.desktop --host ilo.example --user Administrator
# or, in a browser:
.venv/bin/python -m ilo3console.bridge --listen 127.0.0.1:8080
```

Full details, including the legacy-TLS quirk and virtual-media flows, are in
[docs/PROTOCOL_ILO3.md](docs/PROTOCOL_ILO3.md).

## iLO 100 (`ilo100console/`)

- `session.py` — HTTP Digest web login → `httpdata` token → socket handshake
- `buffer.py` — little-endian cursor + socket reader
- `protocol.py` — message catalog (handshake, keyboard/mouse, storage)
- `video.py` — text-mode and graphics-mode (raw + compressed) decoder
- `keymap.py` — USB HID keycode table
- `power.py` — chassis power control over the web UI's CGI page
- `vmedia100.py` — virtual media: listens for the iLO's own inbound connection
- `client.py` — orchestrates a full session
- `desktop.py` + `web/` — native desktop app, connects directly
- `bridge.py` — WebSocket bridge for the in-browser UI

```bash
.venv/bin/python -m ilo100console.desktop --host ilo.example --user Administrator
# or, in a browser:
.venv/bin/python -m ilo100console.bridge --listen 127.0.0.1:8081
```

Full details, including the auth flow, wire framing, and message catalog, are
in [docs/PROTOCOL_ILO100.md](docs/PROTOCOL_ILO100.md).

## Setup

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install cryptography pywebview   # + websockets for the browser mode
```

`cryptography` is only used by `ilo3console` (AES + legacy TLS); `ilo100console`
needs nothing beyond the standard library for its own auth/crypto (plain
Digest auth + stdlib `ssl`). The **native desktop app** (either package) needs
only `pywebview`; the **browser** mode needs `websockets`.

## Use a client directly (headless)

```python
from ilo3console.client import ILoConsoleClient
from ilo3console import input as inp

client = ILoConsoleClient("ilo.example", username="Administrator", password="…")
client.on_frame = lambda: None  # client.framebuffer holds the latest image
client.connect()
client.send_ctrl_alt_del()
client.send_power(inp.POWER_RESET)
...
client.close()
```

The iLO 100 client (`ilo100console.client.ILo100Client`) exposes the
equivalent surface, plus `mount_media()`/`unmount_media()` for virtual media
and power helpers from `power.py`.

## Tests

```bash
.venv/bin/python -m pytest
```

The iLO 3 suite verifies the ciphers against RFC 6229 / NIST vectors, every
input frame byte-for-byte, the handshake state machine, and the DVC decoder
(unit + fuzz); its end-to-end path against live hardware is not yet
validated — see the status note at the end of
[docs/PROTOCOL_ILO3.md](docs/PROTOCOL_ILO3.md). The iLO 100 path has been validated
end-to-end against live hardware — see the status note at the end of
[docs/PROTOCOL_ILO100.md](docs/PROTOCOL_ILO100.md).

## Legacy TLS (iLO 3 only)

iLO 3 only speaks TLSv1.0 with legacy renegotiation and weak ciphers, which
OpenSSL 3 rejects by default. `ilo3console.session.legacy_tls_context()`
relaxes this in Python; `openssl-legacy.cnf` is the equivalent for OpenSSL
CLI tools.
