# HPE iLO 3 Integrated Remote Console (IRC) protocol

This documents the full remote console protocol used by iLO 3 firmware 1.94
on a ProLiant DL380 G7, as implemented in `ilo3console/`.

## Overview

The remote console uses **three** network connections to the iLO:

1. **HTTPS/JSON** (port 443) — mint a session token from credentials, then
   fetch connection parameters and drive virtual-media/power via JSON.
2. **Console channel** (TCP, default port 23 / `rc_port`) — telnet-framed
   handshake, then a one-way **DVC video** stream from the iLO and keyboard /
   mouse / power **input** to the iLO. After a handshake the byte stream is
   XOR-encrypted with an in-protocol stream cipher.
3. **Command channel** (TCP, same host/port as the console) — a second socket
   carrying out-of-band notifications: power state, health LED, POST complete,
   seize / share / firmware-upgrade / unlicensed events.

The original iLO web UI hands the built-in remote console client its session
token and connection parameters up front. A standalone Python client must
instead **log in over HTTPS itself** to obtain the token, then hit the same
JSON endpoints.

## 1. Session token and JSON layer

### TLS quirk

iLO 3 firmware only speaks legacy TLS (TLSv1.0) with legacy renegotiation and
low cipher security levels. OpenSSL 3 refuses this by default; see
`openssl-legacy.cnf` at the repo root (`SECLEVEL=0`, `MinProtocol=TLSv1`,
`UnsafeLegacyRenegotiation`). The Python client must build an SSL context with
these relaxations and certificate verification disabled (self-signed iLO cert).

### Session cookie

Every JSON request carries the token as a cookie:

```
Cookie: sessionKey=<TOKEN>
```

The token is a 32-hex-char value (e.g. `4df94a01ba00faa496305d5c2ec4bc89`). A
standalone client obtains it by POSTing credentials to the iLO login endpoint
and capturing the resulting `sessionKey` cookie, exactly as a browser session
would.

### Credential login (standalone client)

To support user/password auth directly, the client logs in itself:

```
POST /json/login_session
Content-Type: application/json

{"method": "login", "user_login": "<user>", "password": "<pass>"}
```

On success the iLO sets a `sessionKey` cookie and returns a JSON body carrying
the `session_key`; either is captured as the token. `ilo3console.session`
accepts both a username/password (`ILoSession.login`) and a pre-minted token
(`ILoSession.from_token`). The exact login endpoint/method may vary across iLO 3
firmware revisions; `session.py` centralises it in `_do_login` so it can be
adjusted once validated against the live controller.

### Endpoints (`https://<ilo>/json/<name>`)

- `GET /json/rc_info` — connection parameters (see below).
- `GET /json/session_info` — privileges: `reset_priv`, `virtual_media_priv`.
- `GET /json/vm_status` — virtual-media state; `options` array (index 0 =
  floppy/USB, index 1 = CD/DVD) with `vm_connected` / `vm_url_connected`.
- `POST /json/vm_status` — virtual-media control; body is JSON with a `method`
  of `set_virtual_media_options`, a `device` (`FLOPPY`/`CDROM`), a `command`
  (`INSERT` / `EJECT` / `SET`), an optional `url`, `boot_option` (`CONNECT`), and
  `session_key`. HTTP 200 = success; a body containing `SCSI_ERR_NO_LICENSE`
  signals a licensing failure.

The JSON responses are flat and predictable:

- `rc_info` is a single `{...}` object of `"key":value` pairs.
- Arrays are a flat list of `{...}` objects joined by `},{`.

### `rc_info` fields

| Field | Meaning |
|-------|---------|
| `enc_key` | 32-hex-char (16-byte) key. XOR-obfuscates the session key in the handshake, and seeds the console stream cipher. |
| `rc_port` | TCP port for the console + command channels (default 23). |
| `vm_key`, `vm_port` | Virtual-media channel key/port. |
| `optional_features` | Substring flags: `ENCRYPT_KEY`, `ENCRYPT_VMKEY`. |
| `server_name`, `ilo_fqdn` | Display only. |
| `blade`, `enclosure`, `bay` | Blade/enclosure info (0/absent on the DL380 G7 rackmount). |

`enc_key` arrives as hex text; it is parsed into 16 raw bytes.

## 2. Console channel handshake

1. Open TCP to `host:rc_port`. Set `TCP_NODELAY` and `SO_LINGER(true, 0)`.
2. Read one byte. It **must** be `0x50` ('P', the "hello" byte). Anything else
   is a connection failure.
3. Send the **remote-connection request**:
   - Two command bytes, little-endian, from the value `0x2001` (8193):
     `byte[0] = 0x01`, `byte[1] = 0x20`.
   - Followed by the 32-byte **session key** (the ASCII bytes of the session
     token).
   - If `optional_features` contains `ENCRYPT_KEY`, the session key is
     XOR-obfuscated before sending: `sessionKey[i] ^= enc_key[i % len(enc_key)]`
     where `enc_key` is used as its **ASCII hex string**, not the raw bytes.
     Then set a flag bit in `byte[1]`: `| 0x40` if `ENCRYPT_VMKEY` is present,
     else `| 0x80`.
4. Read one response byte:

   | Byte | Meaning |
   |------|---------|
   | `0x51` (81, 'Q') | Access denied → abort. |
   | `0x52` (82, 'R') | Authenticated → proceed, licensed. |
   | `0x53`/`0x59` (83/89) | Authenticated but busy → negotiate seize/share. |
   | `0x57` (87, 'W') | No license → abort (unlicensed). |
   | `0x58` (88, 'X') | No free sessions → abort. |

5. **Busy negotiation:** to seize, send command word `0x0055`
   (`byte[0]=0x55, byte[1]=0x00`); to share, send `0x0056`. After a seize the
   server replies again with `0x51` (denied) or `0x52` (granted).
6. On success, start the receive loop and open the command channel.

### Command-channel handshake

Second socket to the same host/port. Read the `0x50` hello byte, then send:

- Two bytes `byte[0] = 0x02`, `byte[1] = 0x20`, then the 32-byte session key
  (same `ENCRYPT_KEY` XOR-obfuscation and `0x40`/`0x80` flag rule as above).
- Read one byte; `0x52` ('R') = success.

So the distinguishing field between the console and command connections is the
first command byte: **0x01 = console**, **0x02 = command**.

## 3. In-protocol encryption (console channel)

The console stream starts in cleartext. The receive loop watches for escape
sequences:

- `ESC [ R` (0x1B 0x5B 0x52) — **enable** stream encryption from the next byte.
- `ESC [ r` (0x1B 0x5B 0x72) — **disable** stream encryption.

Which cipher is active is set by the DVC `set_encryption` command
(§5, command 12/13): `0 = none`, `1 = RC4-128`, `2 = AES-128`, `3 = AES-256`.

When encryption is active, **every** byte — received video and transmitted
input alike — is XORed with the next keystream byte from the active cipher.
The two directions share one keystream instance per direction:

- **Decrypt** stream (server→client): keyed with `session_decrypt_key`.
- **Encrypt** stream (client→server): keyed with `session_encrypt_key`.

Both keys are set to the raw 16 bytes of `enc_key`. The keystreams are:

- **RC4-128**: standard RC4 PRGA, 16-byte key.
- **AES-128 / AES-256**: OFB keystream with an all-zero IV — each 16-byte
  keystream block is `AES-ECB(key, previous_block)` starting from zeros.

See `ilo3console/crypto.py` for the byte-exact implementation, verified
against published known-answer vectors.

## 4. Input protocol (client → server)

All input messages are short binary records sent on the console channel
(encrypted if active). The first byte is the record type.

### Keyboard — USB HID (10 bytes)

Standard 8-byte USB HID keyboard report wrapped in a 10-byte frame:

```
[0]=0x01  [1]=0x00  [2]=modifiers  [3]=0x00  [4..9]=up to 6 HID keycodes
```

- `[2]` modifier bitmap: bit0=LCtrl, bit1=LShift, bit2=LAlt... Windows virtual
  key codes map to HID usage codes via a 256-entry table, and modifier HID
  codes (`0xE0..0xE7`) are folded into `[2]` as `1 << (hid & 0x1F)`.
- A key release is signalled by resending the report with that key removed;
  an all-zero report `[0x01,0,0,0,0,0,0,0,0,0]` releases everything.
- Convenience combos build the report directly, e.g. Ctrl-Alt-Del sends
  modifiers `[2]=0x05` (LCtrl|LAlt) with HID `[4]=0x4C` (Delete), then clears.
  Num Lock = HID `0x53`, Caps Lock = `0x39`, Ctrl-Alt-Backspace = mod `0x05`
  key `0x2A`, Ctrl-Alt-Fn / Alt-Fn use HID `0x3A..0x45` (F1..F12).

### Mouse — absolute (10 bytes)

```
[0]=0x02 [1]=0x00 [2..3]=x (LE) [4..5]=y (LE) [6]=dx [7]=dy [8]=buttons [9]=0
```

Absolute coordinates are scaled to a 0..3000 range:
`x_wire = 3000 * x_pixel / screen_x` (same for y). `dx`/`dy` are signed relative
deltas clamped to `[-127, 127]`. Button bitmap: left=`0x02`, center=`0x04`,
right=`0x01` (note: not the natural 1/2/4 order).

### Power (4 bytes)

```
[0]=0x00 [1]=0x00 [2]=option [3]=0x00
```

`option`: `0`=momentary press, `1`=press-and-hold, `2`=power cycle,
`3`=system reset. Each sets `post_complete=false` first.

### Screen refresh / ack

- Refresh: 2 bytes `[0x05, 0x00]`.
- Ack (answering DVC command 16): 2 bytes `[0x0C, 0x00]`.

## 5. DVC video codec (server → client)

The video is a proprietary bit-stream ("DVC") decoded into 16×16 pixel
blocks. It is a **bit-level state machine**, fed one byte at a time (after
decryption).

### Bit mechanics

Bits accumulate LSB-first into an accumulator (`acc |= byte << bcnt; bcnt +=
8`). Pulling `n` bits then **bit-reverses** them through a precomputed table
(so codes are read MSB-first within each field). A running count of
consecutive zero bits over 30 forces a resync (hunt state).

### State machine

48 states (state 0 … state 47). Each state says how many bits to consume; the
next state is chosen by the decoded bit value (one transition for code==0,
another for code!=0). Key states:

- **Color cache / LRU**: up to 17 recently-used colors. Pixels are coded
  either as an explicit RGB triple (grey/RGB-red/RGB-green/RGB-blue states,
  8/9/41/42) or as a cache index whose bit-width shrinks as the cache fills
  (states 38/4/5/6/7/32). Cache entries age out after each completed block.
- **Pixel runs** (states 12/13/14): run-length repeats of the last color into
  the current block.
- **Block placement** (move-xy / move-short-x / move-long-x / block-repeat
  states): set the current block (x,y) in 16-px units and copy/repeat
  completed blocks into the framebuffer. With block height 16, x/y are masked
  to 7 bits (`& 0x7F`).
- **Color depth**: bits-per-color is 5, 4, 3 or 2 bits per channel. A
  remap table maps the packed N-bit-per-channel value to a 24-bit RGB pixel.
  Default is 5 bits (15-bit color).
- **Mode / size** (states 26/40/47): size in blocks (x × y); screen pixel size
  is `size_x*16 × size_y*16 (+ clip)`. "Half-height" mode (8-px blocks) kicks
  in above 1616 px width on capable systems.

### Embedded commands (opcode collected in a dedicated state, terminated in
another)

The stream can carry side-band commands:

| Op | Action |
|----|--------|
| 1  | Exit DVC mode. |
| 2  | Print channel follows (status text / console text). |
| 3  | Set framerate. |
| 4  | Power status = on. |
| 5  | Power status = off; clear screen. |
| 6  | POST/"no signal"; clear screen, reset status. |
| 7  | Terminal-services type. |
| 9  | Key-change: flush partial bits (RC4 key roll boundary). |
| 10 | Seize — halt console. |
| 11 | Set bits-per-color. |
| 12 | Set encryption cipher (none/RC4/AES128/AES256). |
| 13 | Header: bits-per-color, encryption, license flags, capability flags. |
| 16 | Request ack — client replies `[0x0C,0x00]`. |
| 128| Repaint. |

Print channels: channel 1/2 → status fields, 3 → log, 4 → on-screen text
overlay.

## 6. Command channel messages (server → client)

Fixed 12-byte header per message: `[0]=type`, `[4]=size`, `[8..9]=field`,
`[10..11]=field`, followed by `size` payload bytes for some types.

| Type | Meaning / payload |
|------|-------------------|
| 2  | POST complete. |
| 3  | Power state (1 byte): 0=off else on. |
| 4  | Health LED (1 byte): 0=green,1=yellow,2=red,else off. |
| 5  | Pre-POST progress code (2 bytes) → status field. |
| 6  | Seize request (128 bytes: two 64-char strings, user + host). Client replies with a boolean (`[0x04..]`=yes / `[0x03..]`=no). |
| 7  | VM ack (4 bytes: status, device, ?, connected) → updates VM menu locks. |
| 9  | Share notification (128 bytes). |
| 10 | Firmware upgrade in progress → halt. |
| 11 | Unauthorized action (field[10] selects: 2=RC, 3=VM, 4=power). |
| 13 | VM notification (1 byte). |
| 14 | Unlicensed → shutdown. |
| 15 | Reset → shutdown. |

Boolean replies use 4 bytes: `[0x04,0,0,0]` = true, `[0x03,0,0,0]` = false.

## 7. Virtual media

Two independent mechanisms, matching the iLO web UI's Virtual Devices menu.

### 7a. URL media (iLO fetches the image itself)

Pure JSON, over the same HTTPS session as §1 — no extra TCP connection. This
is the "Connect URL Media" flow:

```
POST /json/vm_status
{"method":"set_virtual_media_options","device":"FLOPPY"|"CDROM",
 "command":"INSERT","url":"<http(s) URL>","session_key":"<token>"}
```

then, if that returns `"Success"`, a second call actually connects it:

```
POST /json/vm_status
{"method":"set_virtual_media_options","device":"FLOPPY"|"CDROM",
 "command":"SET","boot_option":"CONNECT","url":"<url>","session_key":"<token>"}
```

`"command":"EJECT"` (no `url`) unmounts. A response body containing
`SCSI_ERR_NO_LICENSE` means the iLO's virtual-media license is missing/expired.
`GET /json/vm_status` reports current state: an `options` array, index 0 =
floppy/USB, index 1 = CD/DVD, each with `vm_connected` / `vm_url_connected`.
Implemented in `session.py` (`vm_status`, `insert_url_media`, `eject_url_media`).

### 7b. Local-file media (we act as a SCSI target)

A **second TCP connection** to `rc_info.vm_port` (default 17988). Direction is
inverted from the console/command channels: here **the client is the SCSI
target** and the iLO is the initiator, issuing SCSI CDBs to read (and, for
floppy/USB, write) blocks of a local disk image; iLO firmware then presents
that as an attached USB/SCSI device to the physical server. This is the
"Browse for image file" flow.

**Handshake** (no `0x50` hello byte on this channel, unlike §2):

- Client sends 34 bytes: `[0x10, device | 0x80, key(32)]`.
  - `device`: `1`=floppy, `2`=CD/DVD, `3`=USB key. The `0x80` bit means "not a
    real local device" — always set here, since a headless client never has a
    physical floppy/USB drive to hand off.
  - `key` = the same 32-byte session token as the console handshake (**not**
    `vm_key`), XOR-obfuscated the same way but gated by a *different* flag:
    `ENCRYPT_VMKEY` in `optional_features` (independent of `ENCRYPT_KEY`).
  - Floppy images over `0x2D0000` bytes (2880 KiB, the largest real floppy
    size) are announced as `device=3` (USB key) instead of `1` — chosen by
    image size.
- iLO replies 4 bytes: `[0x20, 0x00, ver_minor, ver_major]` on success; any
  other first byte is a rejection.

**SCSI-target loop**, after the handshake, on the same socket: the iLO sends
12-byte SCSI CDBs; the client replies with a 16-byte reply header (magic
`DE C0 AD 0B`, little-endian `flags` int32, `media`/`sense_key`/`asc`/`ascq`
bytes, little-endian `length` int32) plus `length` bytes of payload for reads.
A CDB with opcode `0xFE` is a keepalive ping — echo `req[4:12]` back verbatim
in the reply's `media..length` bytes and keep waiting; it doesn't count as a
real command. If the connection goes quiet for a while with no bytes at all
in transit either direction, an empty keepalive-flagged reply is sent. Two
backing types:

- **CD/DVD**: read-only, 2048-byte blocks. Handles `TEST_UNIT_READY`,
  `READ_CAPACITY`, `READ_10`/`READ_12`, `READ_TOC`, `MODE_SENSE`,
  `GET_EVENT_STATUS`, `PREVENT_ALLOW_MEDIA_REMOVAL`, `START_STOP_UNIT`.
  Unhandled opcodes get an ILLEGAL REQUEST reply.
- **Floppy / USB key**: same device class for both — which one was announced
  in the handshake is the only difference. Read-write, 512-byte blocks. Adds
  `WRITE_10`/`WRITE_12`/`WRITE_VERIFY`, `READ_CAPACITIES`, `FORMAT_UNIT` (a
  no-op success for a fixed-size backing file rather than an actual
  reformat). Unhandled opcodes get **no reply at all**.

A `TEST_UNIT_READY`/read/write's sense state progresses not-ready (0, no
media) → media-changed (1, first poll after becoming ready) → ready (2)
across successive requests.

Implemented in `vmedia.py` (`VirtualMediaChannel`, `ReplyHeader`, `FileMedia`);
wired into `client.py` (`mount_local_image`/`unmount_local_image`). Native
desktop only (`desktop.py`, via pywebview's file picker) — the browser bridge
doesn't expose this, since it's Python opening a file on its own machine and
serving it to the iLO, which only makes sense when Python and the picked file
share a filesystem (true for the desktop app; not generally true across the
browser/bridge boundary). The browser gets URL media (§7a) instead.

**Not implemented** (native-device / physical-hardware integration,
meaningless for a headless cross-platform Python client): enumerating and
reading real local floppy/USB/CD drives, passthrough to a physical CD drive,
and a wizard for creating a blank formatted floppy image file — out of scope;
users can create an image with any standard tool and mount it via
`mount_local_image`.

## Implementation status

- [x] `crypto.py` — RC4 + AES-OFB keystreams (verified against RFC 6229 / NIST).
- [x] `session.py` — HTTPS legacy-TLS login (user/pass or token), `rc_info`/JSON.
- [x] `console.py` — console-channel handshake + encrypted byte pump.
- [x] `dvc.py` — DVC video decoder → framebuffer.
- [x] `cmdchannel.py` — command-channel notifications.
- [x] `input.py` — keyboard/mouse/power encoding.
- [x] `client.py` — orchestrator tying the channels + decoder together.
- [x] `png.py` + `bridge.py` + `web/` — WebSocket bridge and browser front-end.

**Unit-verified**: the ciphers against published vectors, every input frame
byte-for-byte, the handshake state machine, and the DVC decoder's
colour/cache/command logic plus a 50k-byte fuzz. **Not yet
validated against live hardware:** the end-to-end auth → encrypted-video path
and the exact `/json/login_session` request shape both need a real controller.
`session._do_login` and the DVC bit-layout are the most likely places to need
adjustment once tested live.
