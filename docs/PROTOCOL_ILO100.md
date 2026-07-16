# HPE iLO 100 / "Mahogany" remote console protocol

This documents the "Mahogany" KVM protocol used by ServerEngines-based HPE
iLO 100-class controllers (lower-end ProLiant ML/DL 1xx), as implemented in
`ilo100console/`. It is a VNC-derived protocol, not iLO 3's telnet/DVC design
(see [docs/PROTOCOL_ILO3.md](docs/PROTOCOL_ILO3.md) for iLO 3); the two share no wire
compatibility.

**Status: validated end-to-end against live hardware** (a real iLO 100
device). Auth, framing, the full message-ID catalog, and the video/text-mode
decoder have all been confirmed via live testing, which also caught one real
bug (see the `ClientNOP` note below) that static documentation reading alone
would not have surfaced. `ilo100console/` implements the console
(video+keyboard+mouse) path.

## Scope decision (confirmed with user)

First pass targets **console only** (video + keyboard + mouse over the single
KVM channel). Virtual media (storage channel) is deferred: unlike iLO 3, the
storage logic isn't openly documented — the console proxies to a native local
helper process (`LIBM2-32/64.SO`, `M2-32/64.DLL`) over loopback, so that wire
format had to be derived from captured loopback/network traffic rather than
from written documentation.

## Overview: one TCP endpoint for console (port 80)

A full port scan of a real device shows only two TCP ports open at rest:
23 (a generic embedded-Linux/IPCOM telnet management shell, unrelated to the
console) and **80**. Port 80 does double duty: it serves the HTTP(S) web
management UI (HTTP Digest auth) *and*, in the same listener, accepts the raw
Mahogany binary protocol — there is no separate socket, no HTTP Upgrade, no
protocol-switch handshake. You just open a bare TCP connection to port 80 and
immediately start sending/receiving Mahogany's binary frames; the server
apparently distinguishes HTTP requests from Mahogany connections purely by
the first bytes each speaks. This was confirmed by a live packet capture: the
entire KVM session (auth handshake + ~1.5MB of continuous video updates) rode
over one uninterrupted TCP stream to port 80, with zero HTTP framing anywhere
in it, from the very first byte after the TCP handshake.

The `NonSecure_KVMPort=80` connection parameter is not a fallback/default — it
is the real, correct value the live device uses. (An earlier version of this
doc speculated the value must be wrong or that the hostname was behind a
reverse proxy; both were wrong turns during debugging — the actual bug was in
the client's own framing, below.)

Two session types: `"kvm"` (plain TCP, the path validated live) and `"kvmssl"`
(whole socket wrapped in SSL, with a trust-everything certificate check — not
yet tested live). **Unlike iLO 3, there is no separate in-band stream cipher**
(no RC4/AES-equivalent) — encryption, when used, is just the standard SSL
wrapping of the whole socket.

Ports `5901`/`5902`/`5904` (storage/keyboard) are unrelated to the console
path validated here; the storage socket connects to **localhost**, not the
iLO, talking to the native local helper — out of scope per the scope decision
above.

## Auth: fetched from an authenticated HTTPS/Digest web session, then used directly on the socket

There is no separate JSON token-minting endpoint like iLO 3's `rc_info`, but
there **is** a web-login step — this doc's first draft got this wrong (see
below). The real flow, confirmed live:

1. Authenticate to the web UI with **HTTP Digest** auth (`WWW-Authenticate:
   Digest realm="<device>", qop="auth", ...`) — plain HTTP, not HTTPS, on
   this device.
2. `GET /kvms.html` (authenticated). The response embeds connection
   parameters including `ipaddress`, `username`, `NonSecure_KVMPort`, and
   critically **`httpdata`** — a long hex token, freshly minted per page
   load, e.g. `BAED17A9F9C6CE559311713F1901D83CD85C244...` (~96-98 hex chars).
3. Open a bare TCP socket to `ipaddress:NonSecure_KVMPort` (80).
4. Send `ClientNOP` then `ClientHandshake` (see wire formats below) with
   `username` as given and **`httpdata` as the password** — not the
   account's real login password. `session.py` (`fetch_httpdata_token`)
   replicates this: it does the Digest-authenticated `GET /kvms.html` itself
   and extracts `httpdata`. (This step was initially documented but not
   actually wired up in code — the gap surfaced immediately once the desktop
   UI was tested with a real account password instead of a manually
   pre-fetched token: the server silently closed the connection rather than
   hanging, distinct from the `ClientNOP` framing bug's symptom.)
5. Server replies `ServerHandshake` (id `200`): a 32-byte ASCII buffer,
   space-separated fields, where everything after the first space is scanned
   for the characters `k`/`v`/`m`/`s` (case-insensitive) marking which
   privileges (keyboard/video/mouse/storage) are enabled. Live example:
   `"MAHOGANY KVM"` padded to 32 bytes with trailing spaces — session id
   `MAHOGANY`, privileges `KVM` (keyboard+video+mouse, no storage).

**Corrected from the first draft of this doc:** `httpdata` was originally
(and wrongly) assumed to be the literal plaintext account password — it
isn't; it's a per-session token, analogous in role to iLO 3's `RCINFO1`. This
was caught only by testing live.

The client pads before building the handshake: username to exactly **16**
chars, `httpdata`/password to exactly **20** chars (truncated — the live
token is ~96-98 chars, so only its first 20 go in this field), plus a
separate **128**-byte "full token" field carrying the *entire* untruncated
`httpdata` value, also null-padded. Confirmed byte-for-byte against a live
capture: the `password` field is exactly the first 20 characters of the same
string that appears in full (null-padded to 128) in `passwordFull`.

### The bug that actually blocked the connection

Not a wrong port, not a proxy, not a session/IP correlation issue — all
suspected and ruled out during debugging. The real bug: **`ClientNOP` is not
a bare 1-byte message.** It writes a fixed **7-byte all-zero payload** after
the id byte, always zero-filled. This was missed early on because the
message's payload was inferred from the message ID alone rather than
confirmed byte-for-byte. Sending only the 1-byte id shifts every subsequent
byte (the entire `ClientHandshake`) one message-boundary early from the
server's perspective, and the connection just hangs — indistinguishable from
the outside from "wrong port" or "wrong protocol," which is exactly why it
took a live capture to find.

The default `ClientHandshake` fields were also tuned to match the live
capture exactly: `signature` defaults to the embedded-client value
(`0x5A5A5A5A`), and `config_mask` defaults to requesting **every** privilege
(`CFG_EVERYTHING = 31`) — the real client always requests everything
regardless of what it actually uses; the server still only enables what the
account/license allows (confirmed live: `storage=False` was correctly
reported back even though the client requested it).

## Wire framing

Little-endian throughout. Every message: **one byte** command/message ID,
then a message-type-specific payload with no common outer length prefix —
each message type has its own fixed fields, and/or an internal 4-byte
length-prefixed blob for variable payloads (e.g. `BitBlt`'s pixel data). This
is simpler than iLO 3's per-channel handshake-then-stream design — there's no
separate 12-byte-header-plus-payload struct; framing is purely per-message.

```
ClientHandshake:
  int32  signature        (0x5A5A5A5A embedded-client, 0x12121212 standalone)
  int32  userName.length()   (== 16 after padding)
  int32  password.length()   (== 20 after padding)
  int32  clientHandshakeConfig.length()  (always 4)
  int32  key.length()
  bytes  userName          (16 bytes, null-padded)
  bytes  password           (20 bytes, null-padded)
  int32  clientHandshakeConfig  (bitmask: 1=video 2=mouse 4=keyboard
                                  8=storage0 16=storage1, 31=everything)
  bytes  key                (variable; purpose TBD — always "" so far)
  bytes  passwordFull       (128 bytes, null-padded)

ServerHandshake:
  bytes[32]  ascii buffer, non-identifier bytes replaced with space, trimmed;
             substring after the first space is lowercased and scanned for
             'k'/'v'/'m'/'s' -> isKeyboardEnabled/isVideoEnabled/
             isMouseEnabled/isStorageEnabled
```

## Message ID catalog

### Client -> Server

| ID | Message | Notes |
|----|---------|-------|
| 221 | `ClientHandshake` | see above |
| 210 | `ClientNOP` | sent immediately after connecting; 7-byte zero payload |
| 241 | `RequestVesaMode` | ask server to (re)announce video mode |
| 242 | `Invalidate` | request redraw of one or more regions |
| 243 | `InformHLevelCompression` | tell server whether "high-level compression" (encryption?) is on |
| 177 | `ClientAbsoluteMode` | switch mouse to absolute positioning |
| 178 | `ClientRelativeMode` | switch mouse to relative; also carries a "hide mouse" flag |
| 179 | `ButtonStateAtAbsolute` | mouse buttons + absolute x/y |
| 180 | `ButtonStateAtRelative` | mouse buttons + relative x/y |
| 181 | `MouseMove` | x/y only, no button state |
| 193 | `RequestKeyIndicators` | ask server for Caps/Num/Scroll-lock LED state |
| 209 | `KeyStateChange` | one key press/release: scancode + down/up + "pre-translated" flag |
| 211 | `RequestPrimaryControl` | claim exclusive KVM control (iLO 3's "seize" equivalent) |
| 214 | `DisconnectUser` | admin action: disconnect another connected user by id |
| 215 | `RelinquishFullControl` | give up primary/exclusive control |
| 216 | `ClientDisconnect` | graceful disconnect, carries a reason code + message |
| 153 | `StorageClientConnect` | storage channel setup (deferred) |
| 154 | `StorageClientDisconnect` | storage channel teardown (deferred) |
| 217 | `KeyboardTurnScreenOn` | |
| 218 | `KeyboardTurnScreenOff` | |

### Server -> Client

| ID | Message | Notes |
|----|---------|-------|
| 200 | `ServerHandshake` | see above |
| 225 | `InformVesaMode` | mode number, bpp, width, height |
| 226 | `BitBlt` | raw/text-mode screen update — see below |
| 227 | `EnhanceBitBlt` | compressed screen update ("enhanced" codec) — see below |
| 238 | `GraphicsRegisterValue` | read but not acted on |
| 230 | `SetPalette` | palette + attribute table update |
| 234 | `SetTextCursor` | text-mode cursor position + shape (start/stop scanline) |
| 235 | `SpecialGraphicsBit` | blinking-text toggle, double-buffer swap, 4bpp color index |
| 236 | `MatroxGraphicsCursor` | hardware cursor (Matrox chipset) control/data/mode |
| 228 | `StandbyPower` | server display went to standby |
| 213 | `InformKeyIndicators` | Caps/Num/Scroll-lock state, reply to `RequestKeyIndicators` |
| 197 | `MultiUserState` | another user connected/disconnected/took control |
| 198 | `ServerDisconnect` | server is closing the connection; reason + message |
| 201 | `FirmwareVersion` | |
| 137 | `StorageStatus` | storage channel status (deferred) |

## Video: `BitBlt` / `EnhanceBitBlt`

`BiltType` (message subtype) values seen:

```
256  BIT_BLT_COPY                      — raw pixel copy
257  TEXT_ASCII                        — text-mode: character codes only
258  TEXT_ATTRIBUTE                    — text-mode: attribute bytes only
259  TEXT_ASCII_ATTRIBUTE              — text-mode: interleaved char+attr
260  TEXT_FONT                         — font glyph data
261  TEXT_ASCII_FONT
262  TEXT_ATTRIBUTE_FONT
263  TEXT_ASCII_ATTRIBUTE_FONT
264  TEXT_ASCII_ATTRIBUTE_COMBO
496  ENHANCE_BLT_BIT                   — compressed update
-32272 ENHANCE_BLT_BIT_NEW_FRAME       — compressed, starts a new frame
498  ENHANCE_BLT_BIT_HLC               — compressed, "high-level compression" variant
-32270 ENHANCE_BLT_BIT_HLC_NEW_FRAME
499  SPECIAL_BLT_BIT_4BPP              — 4-bit-per-pixel special mode
-32269 SPECIAL_BLT_BIT_4BPP_NEW_FRAME
```

This confirms the device supports both legacy VGA **text mode** (character +
attribute + font-based rendering, for BIOS/POST screens) and a **graphics
mode** (raw copy or a proprietary "enhanced"/compressed codec) — analogous to
iLO 3's DVC codec but structured differently, with an explicit text-mode path
DVC didn't have.

`BitBlt` payload: `biltType`(short) `fontHeight`(byte) `fontWidth`(byte)
`sourceRectangle`(x,y,w,h shorts) `destinationRectangle`(same shape)
`dataSize`(int32) `data`(dataSize bytes) — interpretation of `data` depends
on `biltType`, decoded in `video.py` and confirmed live for the text-mode
path (see Implementation status below).

`EnhanceBitBlt` payload: `biltType`(short) `tileWidth`(byte)
`tileHeight`(byte) `tripletCode`(int32) `repeatCode`(int32) `rawSize`(int32)
`scrunchSize`(int32) then **128 int32s** (two parallel 64-entry
`snoopLowMap`/`snoopHighMap` dirty-tile bitmaps — a spatial dirty-rectangle
scheme, one bit per tile column per row, `low`=columns 0-31, `high`=columns
32-63) then `scrunchSize` bytes of tile data for the dirty tiles only. Three
sub-codecs share this envelope, all implemented in `video.py`:

- **Plain (`BLT_ENHANCE`/`-NEW_FRAME`, 496/-32272)**: raw, uncompressed
  per-pixel bytes for each dirty tile, same BGR[R] byte layout as `BitBlt`'s
  raw-copy mode — just restricted to the tiles the bitmap marks dirty. The
  source cursor still advances a full tile's worth of bytes for a
  vertically-clipped tile (it just doesn't get written).
- **HLC (`BLT_ENHANCE_HLC`/`-NEW_FRAME`, 498/-32270)**: a genuine
  compression codec — per-channel (blue/green[/red], or a single
  palette-index channel for bpp≤8) byte-oriented RLE with two escape bytes
  (`tripletCode` = next byte repeats 3×; `repeatCode` = next byte is a
  repeat count, with count 0/1 as literal escapes for the two special bytes
  themselves). Preceded by 2 or 3 little-endian int32 channel-length headers
  (only for bpp>8) so each channel's compressed sub-stream can be located.
- **4bpp (`BLT_SPECIAL_4BPP`/`-NEW_FRAME`, 499/-32269)**: nibble-packed (2
  px/byte, low nibble first) per-tile attribute indices, resolved through
  the same attribute→palette lookup as VGA text/4bpp mode.

The attribute→palette lookup is the shared VGA attribute-controller → DAC
indirection used by text mode and 4bpp graphics: a small per-pixel attribute
index (0-63) is looked up in a 64-entry attribute table to get a final
256-entry palette index, with a "special 4bpp mode" offset variant gated by
`attributes[16]`'s top bit.

## Keyboard / mouse — confirmed

`KeyStateChange`'s scan code is a literal **USB HID usage id** (e.g. HID
`0x04` is `'a'`; see `keymap.py`). Same standard table as iLO 3; modifiers
(Ctrl/Shift/Alt/GUI, HID `0xE0`-`0xE7`) are sent as ordinary individual
key-down/up events, not folded into a report byte like iLO 3's keyboard
frame — `KeyStateChange` is always exactly one key at a time.

Mouse absolute mode sends raw framebuffer pixel coordinates with **no
scaling**. `ButtonState` is one byte per button, 3 buttons always sent
together (`[0]=left [1]=right [2]=middle` — note the non-sequential
button→array-index mapping), bit0=pressed, bits1-7=trackwheel position
offset by 64 (idle/centered = byte `0x80`).

## Not yet investigated

- Reconnect behavior and less common message-handling edge cases.
- `kvmssl` (SSL session type) — not tested live, only the plain `"kvm"` path.
- Storage/virtual-media wire format (deferred per scope decision above).

## Implementation status

**Validated end-to-end against live hardware:** handshake,
`ServerHandshake` privilege parsing, `InformVesaMode`, text-mode `BitBlt`
(ASCII+attribute+font decode, cursor rendering) all confirmed correct via a
live connection — 264 frame events streamed over 6 seconds, resolved to the
expected 640×400 (80×25, 8×16 font) text mode, rendered to a PNG showing a
correct VGA text cursor on a blank console. `EnhanceBitBlt`/HLC/4bpp paths
are unit-tested against synthetic data (not yet exercised by a live
graphics-mode frame — the live device was sitting at a blank text-mode
prompt for the whole test).

`ilo100console/`: `buffer.py` (LE cursor + socket reader), `protocol.py`
(message catalog), `video.py` (text/graphics decoder), `keymap.py` (HID
table), `session.py` (Digest web login → `httpdata` → socket handshake),
`client.py` (orchestrator), `desktop.py` + `bridge.py` + `web/` (native
webview app and browser bridge, mirroring `ilo3console`'s split — same
direct-connect-vs-WebSocket-bridge reasoning applies here too),
`tools/try_connect.py` (manual smoke-test CLI).

**UI layer fully validated against live hardware, including visual
confirmation in the actual native window:** `desktop.Api.
connect()` with the *real account password* (not a manually pre-fetched
token) → web login → handshake → continuous frame streaming → correct
resize event → live `InformKeyIndicators` updates, all four lamps
(link/kbd/video/mouse) on, status "connected".

### A third real bug, caught only by looking at the actual screenshot

The first screenshot showed a blank console with just a correctly-positioned
cursor — no visible text, even though the underlying `ascii_table`/
`attribute_table`/`font_table` all decoded correctly (verified directly:
real glyph bitmaps, a real 16-color VGA palette from a genuine `SetPalette`
message, and — once dumped as text — an entirely legible screen: a Proxmox
VE login banner). The bug was in `_image_rect`'s pixel-writing loop: the
horizontal pixel cursor was being reset to the start of the scanline
**inside** the per-character loop instead of accumulating across it, so
every character in a row overwrote the same first `font_width` pixels — only
the last character's glyph survived at the very left edge, and the rest of
each row was simply never written. The independently-drawn text cursor was
unaffected (different code path), which is exactly why it rendered fine
while all the actual text vanished — a textbook case of a bug whose symptom
(correct cursor, blank text) doesn't point at its cause (a loop-nesting
mistake in glyph blitting) at all. Fixed; confirmed live with a fully
legible Proxmox VE login screen rendered correctly. A regression test
(`test_text_mode_multiple_cells_advance_horizontally`) was added
specifically because the *existing* single-character test could not have
caught this — the bug only manifests with 2+ characters in a row.

### A fourth real bug: input silently did nothing (view-only by default)

Reported by the user as "I can't type in the console." Mouse appeared to
work (no errors) but was actually equally inert — text-mode consoles have
no visible pointer, so there was nothing to notice. Diagnosed by connecting
directly with `session.py`/`client.py` (bypassing the GUI entirely) and
decoding `MultiUserState` (msg 197, previously discarded after reading the
raw `flags` int): every fresh connection came back with
`user_status == 1` (view-only), even though nothing else was connected and
`allowed_to_take_full_control` was set. The real client gates every
`KeyStateChange` send on not being view-only, and only clears that flag
after the user clicks "OK" on a "request full control?" modal dialog, which
sends `RequestPrimaryControl` (msg 211). Our client had no equivalent — it
sent `KeyStateChange`/`ButtonStateAtAbsolute` byte-correct frames from a
permanently view-only session, and the server silently dropped them
(confirmed live: raw protocol-level key sends produced zero change on
screen, even with correct HID codes and wire framing).

Fixed by adding `MultiUserState.user_status`/`allowed_to_take_full_control`/
`has_full_control` properties (`protocol.py`) and
`ILo100Client._apply_multi_user_state` (`client.py`), which auto-sends
`RequestPrimaryControl` the moment the server offers it — mirroring what a
human clicking "OK" does, appropriate here since this console only ever has
one real user. `on_control_changed` is wired through `desktop.py`/
`bridge.py` into the UI (`app.js` shows "connected (requesting control…)"
in the status bar during the brief window before the grant lands, instead
of leaving input silently inert with no explanation).

Note while live-testing this fix: rapid-fire diagnostic connections from
multiple concurrent scripts appeared to compete with each other for the
single full-control grant (consistent with the dialog-driven arbitration
for a second connecting client) — a real, uncontested single session should
get control quickly once this lands.

### Chassis power control — a separate HTTP/CGI channel, not the KVM socket

The Mahogany protocol itself has no power messages (the only "power"-adjacent
message is `StandbyPower`, a *display* blank/standby state, not chassis
power). Real power control lives in the device's plain HTML/CGI-style web
management UI, reachable with the same Digest auth as
`fetch_httpdata_token`: `GET /chassis.html` renders a "Virtual Power" page
with `Power Status: ON`/`OFF` and a form whose `PwrCtrl` query param drives
the action (`PowerUp`, `PowerCycle`, `SoftPwrDn`, `HardReset`, `PowerDown`,
`ForceDump`) — submitting it (`GET /chassis.html?PwrCtrl=<action>&Button1=
Apply`) re-renders the same page with the new status. Implemented in
`power.py` (`get_power_status()`/`send_power_action()`), wired into
`client.py`/`desktop.py`/`bridge.py`/`web/`, with the same confirm-modal
gating as Ctrl+Alt+Del (both are actions a click can't undo). `ForceDump` is
deliberately not exposed in the UI menu (an obscure diagnostic-dump trigger,
not a normal power action) but is available at the `power.py` level.

This BMC's httpd is old and fragile: it can reset a connection mid-response
(`ConnectionResetError: [Errno 104]`, raw and unwrapped by urllib — it
happens inside `http.client`'s `getresponse()`, which isn't inside urllib's
own try/except) even for a bare unauthenticated `GET /`, then recover a
moment later. Confirmed live twice in this project: once as a hard failure
requiring a BMC power-cycle after a heavy testing session (see project
memory), and once as an intermittent blip severe enough to leave the power
lamp showing a stale/wrong reading after a real power-on because the
status read-back after the action got reset. `get_power_status()` now
retries with backoff (safe — it's read-only); `send_power_action()`
deliberately does *not* retry the action itself (it's a non-idempotent GET,
and blind-retrying could double up a `PowerCycle`/`HardReset`) but falls
back to a retried status check on failure so the caller still gets an
accurate reading instead of one stuck on the pre-action state.

Power can also change through means entirely outside this app's control —
the device's own browser-based web UI (`chassis.html`'s own Apply button),
a physical power button, IPMI, another console instance connected at the
same time. A fetch-once-at-connect model left the lamp and the
power-off screen-blanking (see below) permanently stale whenever that
happened — confirmed live: powering on via the device's own web UI while
this console was connected left it stuck showing a red lamp and "system
powered off" indefinitely. Fixed with a background poller in
`ILo100Client` (`_run_power_poller`, `_POWER_POLL_INTERVAL = 15s`) that
fires an immediate check on connect and then periodically, calling
`on_power_status` only when the status actually changes (wired straight
into the existing `power` UI event, so no separate JS handling was
needed). Kept deliberately gentle (15s, not more aggressive) given this
BMC's httpd is already known to be fragile under connection load.

## Virtual media — derived from a live capture, partially implemented

The scope decision above deferred virtual media because the real storage
logic isn't openly documented — the console proxies to a native helper
binary (`LIBM2-32/64.SO`/`M2-32/64.DLL`). That's still true for the actual
data-tunnel protocol, but a live packet capture (a VM running the real
console client, reconfigured to bridged/macvtap networking so the iLO could
actually dial back into it — see below) surfaced enough of it to implement
a real, if not fully certain, first pass.

### Architecture is inverted from iLO 3

For iLO 3, the client dials the iLO's `rc_info.vm_port` and acts as a SCSI
target. For iLO 100, `StorageClientConnect` (message 153, full wire format
below) tells the iLO an IP:port to dial, and the **iLO's own firmware makes
the outbound connection** — confirmed live: the SYN came from the iLO's IP.
This means a standalone client needs to run a TCP *listener*
(`vmedia100.StorageServer`) rather than an outbound client.

### `StorageClientConnect`/`StorageClientDisconnect`/`StorageStatus` — fully specified

These three console-channel messages (153/154/137) are implemented
byte-for-byte in `protocol.py`: IP address right-aligned in a 16-byte field,
a 2-byte port, per-share index/type/status bytes, and two fixed 512-byte
UTF-16LE share-path buffers (byte lengths, not char lengths, despite what
the length fields inside the message actually count — see
`StorageStatus.readBuffer`'s `s << 1`). Media type codes (0-17, e.g.
`MEDIA_TYPE_CD_ISO_IMAGE = 11`) are data-driven resource-bundle strings
rather than an enum.

### The actual data-tunnel wire format — derived live, not from written spec

Captured a real mount + boot-time read sequence (host powered on, so its
BIOS/OS actually queried the virtual CD-ROM — powering off makes this
channel go completely silent since nothing ever issues a SCSI command).
Confirmed structure:

- **Hello** (536 bytes, iLO→listener, sent right after the TCP handshake):
  an 8-byte header, the ASCII tag `VENDORID`, then a UTF-16LE share path
  matching exactly what was sent in `StorageClientConnect` — the iLO is
  confirming which share it's asking for. Followed by an 8-byte all-zero
  "ready" ping from the iLO, then a 16-byte reply from the listener side
  (values captured verbatim from a real, working session since their exact
  semantics — likely a block-size + session-token pair — aren't
  independently confirmed).
- **Per-command request** (fixed 76 bytes): a handful of per-connection
  opaque handle fields (constant within one connection, semantics
  unconfirmed — likely internal identifiers), a monotonically incrementing
  client-side sequence number at offset 12, a declared
  response-data-length at offset 24, and — critically — **a standard
  12-byte SCSI CDB at offset 64**, byte-for-byte the same encoding already
  implemented and tested in `ilo3console.vmedia` (confirmed live: `TEST_UNIT_
  READY` → `REQUEST_SENSE` → `START_STOP_UNIT` → `READ_TOC` → `READ_10(LBA=
  17, length=1 block)`, the standard SCSI CD-ROM probe sequence, with
  `READ_10`'s LBA/transfer-length fields matching standard big-endian SCSI-3
  encoding exactly). This is the single most useful finding: the SCSI
  command *semantics* didn't need re-deriving at all, only this outer frame.
- **Reply** (fixed 64-byte header + 0 or more bytes of data): offset 24 is
  the data-payload length that follows (confirmed: 0 for status-only, 17 for
  `REQUEST_SENSE`'s sense data, 12 for `READ_TOC`, 2048 for a 1-block CD-ROM
  read). Verified the actual 2048-byte `READ_10` payload contained the
  literal text `"Remote Iso CDROM2.04"` at the ISO9660 volume-descriptor
  sector — proof the framing understanding is correct end-to-end, not just
  structurally plausible.

Implemented in `vmedia100.py`: `StorageServer` (the listener),
`MediaChannel` (hello handshake + request/reply loop), and CD-ROM SCSI
command handlers shared with `ilo3console.vmedia`'s proven logic (same
opcodes: `TEST_UNIT_READY`, `REQUEST_SENSE`, `READ_CAPACITY`, `READ_TOC`,
`READ_10`/`READ_12`, `MODE_SENSE`, `GET_EVENT_STATUS`,
`START_STOP_UNIT`/`PREVENT_ALLOW_REMOVAL` as status-only no-ops). Wired
into `client.py` as `mount_media()`/`unmount_media()`.

### Open problem: `MultiUserState.isAllowedToDoStorage` won't flip true for a fresh client session

`ServerHandshake.storage` (the field we already decode) is **not** the
gate the real client actually uses for `StorageClientConnect`: the initial
handshake flag gets **overwritten** on every `MultiUserState` message by its
own `isAllowedToDoStorage` bit (`MultiUserState` bit `0x100`), and it's this
final, MultiUserState-driven value that actually gates sending
`StorageClientConnect`.

Live testing found this bit **staying false** for this project's own
`ILo100Client` sessions throughout — even after full control was granted,
even with an "LO100 Advanced" license installed on the device (checked via
`lkey.html`) — while a real console client, using the exact same account on
the exact same device, evidently *did* get it to flip true at some point
(that's the only way its `StorageClientConnect` could have reached the VM
at all, since the message is gated the same way client-side). What causes
the flip is not yet confirmed. `client.py`'s `mount_media()` tracks this
flag (`self._storage_allowed`) but does not yet block on it — if it's still
false, the server will most likely just silently ignore the
`StorageClientConnect`. This needs either: a live test with full control
held for longer / under different conditions, or investigating whether a
specific account/user permission (`user.html`) or per-user checkbox governs
it.

### Unverified: the from-scratch `MediaChannel` server has not completed an end-to-end mount

Byte-level structure is confirmed against a real capture, and `vmedia100.py`
is unit-tested (SCSI command handlers produce structurally correct
replies), but a live test where *our own* `StorageServer` actually served a
real mount to the iLO was blocked by the `isAllowedToDoStorage` problem
above before it could be exercised end-to-end. The opaque per-connection
handle fields in the request/reply frames (untested: whether the iLO
validates them or treats them as freely-assignable session tokens) are the
main remaining risk to a real mount actually working once the permissions
issue is resolved.
