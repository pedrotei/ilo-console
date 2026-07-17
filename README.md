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

## Launcher (`launcher/`)

A saved-server list — add/edit/remove named connections (host, username,
generation, port/SSL) and hit **Connect** to open that server's console,
instead of typing `--host`/`--user` every time.

```bash
.venv/bin/python -m launcher.desktop
# or, in a browser:
.venv/bin/python -m launcher.bridge --listen 127.0.0.1:8079
```

It's a thin front end over the existing per-generation apps, not a
reimplementation: "Connect" spawns the matching `ilo3console`/`ilo100console`
desktop app or bridge as its own subprocess, pre-filled with the saved
host/username via the prefill mechanism both already support. Servers are
stored locally at `~/.config/ilo-console/servers.json` (or
`$XDG_CONFIG_HOME/ilo-console/servers.json`).

**Passwords are opt-in and never touch disk.** Checking "Save password" on a
server stores it in the OS keyring (Keychain / Secret Service / Credential
Manager, via the `keyring` package — install the `launcher` extra) rather
than the JSON file, which only ever holds a `has_saved_password` flag. A
saved password is only usable from the **native launcher**
(`launcher.desktop`): it's handed to the child app via an environment
variable (not a CLI argument, which `ps` would expose to other local users)
and that app auto-submits its own connect dialog. The **browser launcher**
(`launcher.bridge`) never auto-fills a password — the prefill it hands the
console page is a URL query string, and a password has no business landing
in browser history or a server access log — so browser-launched consoles
still prompt for it every time regardless of what's saved.

### Certificate monitoring + renewal (iLO 3)

Every saved iLO 3 server's HTTPS certificate is checked once a minute in the
background (`launcher/certmonitor.py`) — a lock icon on its row shows OK
(green), expiring within 14 days or self-signed (amber), or expired/unreachable
(red). Clicking that icon opens a dialog with the certificate's current
subject/issuer/validity and a **Renew via Let's Encrypt** button (hidden right
after a successful renewal, since there's nothing left to do until the next
one's due).

Renewal gets a real certificate onto the iLO end to end: it asks the iLO to
generate a CSR, satisfies Let's Encrypt's **DNS-01** challenge by creating the
`_acme-challenge` TXT record via the **Cloudflare API** (iLO hostnames are
typically internal, so HTTP-01 isn't viable), gets the certificate issued,
imports it, and resets the iLO's management processor so the new certificate
actually takes effect (~30-60s, during which the iLO is briefly unreachable —
the managed server itself is unaffected). Progress streams live into the
dialog.

**One-time setup**, via the gear icon in the launcher header:
- A **Cloudflare API token** (Zone → DNS → Edit + Zone → Zone → Read,
  scoped to the zone covering your iLO hostnames) — saved to the OS keyring,
  same as server passwords.
- **Contact email** (optional, used for the Let's Encrypt account) and the
  **CSR subject fields** (country/state/locality/organization) the iLO's own
  certificate form asks for.

This is implemented entirely on the same `/json/*` HTTP API the rest of
`ilo3console` already speaks (`ilo3console/session.py`'s `request_csr`,
`import_certificate`, `reset_ilo`) plus a small hand-rolled ACME v2 client
(`ilo3console/acme.py`) and Cloudflare DNS client (`launcher/cloudflare.py`) —
no RIBCL, no extra dependencies beyond `cryptography` (already required for
`ilo3console`). See [docs/PROTOCOL_ILO3.md](docs/PROTOCOL_ILO3.md)'s
"Certificate management" section for the full request/response shapes,
confirmed against live hardware.

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
- `certcheck.py` — read-only TLS certificate inspection (expiry, issuer)
- `acme.py` — hand-rolled ACME v2 (RFC 8555) client for Let's Encrypt
- `certrenew.py` — orchestrates `session.py`'s CSR/import/reset calls + `acme.py`
  + a caller-supplied DNS-01 provider into one certificate renewal

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

## Install (on another machine)

Requires Python 3.10+ already on the target machine — nothing else needs to
be pre-installed from PyPI (see the system-dependency note below).

```bash
# From a copy of this repo (e.g. after cloning it there):
pipx install ".[all]"

# Or straight from GitHub, without cloning it yourself first:
pipx install "git+https://github.com/pedrotei/ilo-console#egg=ilo-console[all]"
```

`pipx` builds an isolated venv for the app and puts its commands on `PATH`
(`pipx install pipx` or your OS package manager if you don't have it yet;
plain `pip install --user ".[all]"` works too, just without the isolation).
`[all]` pulls in every optional extra (`cryptography`, `websockets`,
`pywebview`, `keyring`) — installers wanting only one mode can pick a
narrower extra combo (`.[ilo3,desktop]` for just the iLO 3 native app, etc.,
matching the extras defined in `pyproject.toml`).

This installs six commands:

| Command | Runs |
|---|---|
| `ilo-console-launcher` | Native launcher (saved-server list) |
| `ilo-console-launcher-web` | Launcher's browser mode |
| `ilo3-console` | iLO 3 native console (`--host`/`--user`) |
| `ilo3-console-web` | iLO 3 browser-mode WebSocket bridge |
| `ilo100-console` | iLO 100 native console (`--host`/`--user`) |
| `ilo100-console-web` | iLO 100 browser-mode WebSocket bridge |

**System dependency for the native apps (`ilo-console-launcher`,
`ilo3-console`, `ilo100-console`):** `pywebview` needs an OS-provided webview
component that `pip`/`pipx` can't install for you. Windows (10/11) and macOS
already have theirs (WebView2, WKWebView) — nothing extra to do there. On
Linux, install WebKitGTK first, e.g. on Debian/Ubuntu:
`sudo apt install python3-gi gir1.2-webkit2-4.1` (package names vary by
distro/WebKitGTK version — see
[pywebview's docs](https://pywebview.flowrl.com/guide/installation.html) if
that exact package isn't found). The **browser-mode** commands
(`*-web`) don't need this at all — they're plain HTTP/WebSocket servers you
open in any browser.

## Setup (working on this repo)

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
