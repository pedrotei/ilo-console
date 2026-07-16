"""Mahogany message catalog: encode (client->server) and decode (server->client).

There is no common outer envelope — each message is ``[1-byte id][type-specific
payload]`` with no length prefix, so every message type the server can send
**must** be decoded exactly (even ones we don't act on) or the byte stream
desyncs permanently. See ``docs/PROTOCOL_ILO100.md`` for the full
field-by-field derivation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .buffer import LEBuffer, SocketReader

# -- message ids --------------------------------------------------------------

# Client -> server
MSG_STORAGE_CLIENT_CONNECT = 153
MSG_STORAGE_CLIENT_DISCONNECT = 154
MSG_CLIENT_ABSOLUTE_MODE = 177
MSG_CLIENT_RELATIVE_MODE = 178
MSG_BUTTON_STATE_AT_ABSOLUTE = 179
MSG_BUTTON_STATE_AT_RELATIVE = 180
MSG_MOUSE_MOVE = 181
MSG_REQUEST_KEY_INDICATORS = 193
MSG_KEY_STATE_CHANGE = 209
MSG_CLIENT_NOP = 210
MSG_REQUEST_PRIMARY_CONTROL = 211
MSG_DISCONNECT_USER = 214
MSG_RELINQUISH_FULL_CONTROL = 215
MSG_CLIENT_DISCONNECT = 216
MSG_KEYBOARD_TURN_SCREEN_ON = 217
MSG_KEYBOARD_TURN_SCREEN_OFF = 218
MSG_CLIENT_HANDSHAKE = 221
MSG_REQUEST_VESA_MODE = 241
MSG_INVALIDATE = 242
MSG_INFORM_HLEVEL_COMPRESSION = 243

# Server -> client
MSG_STORAGE_STATUS = 137
MSG_MULTI_USER_STATE = 197
MSG_SERVER_DISCONNECT = 198
MSG_SERVER_HANDSHAKE = 200
MSG_FIRMWARE_VERSION = 201
MSG_INFORM_KEY_INDICATORS = 213
MSG_INFORM_VESA_MODE = 225
MSG_BIT_BLT = 226
MSG_ENHANCE_BIT_BLT = 227
MSG_STANDBY_POWER = 228
MSG_SET_PALETTE = 230
MSG_SET_TEXT_CURSOR = 234
MSG_SPECIAL_GRAPHICS_BIT = 235
MSG_MATROX_GRAPHICS_CURSOR = 236
MSG_GRAPHICS_REGISTER_VALUE = 238

# ClientHandshake signature (embedded-in-browser vs standalone -- cosmetic;
# the protocol behaves identically either way as far as the wire goes
# beyond this field itself)
HANDSHAKE_SIG_EMBEDDED = 0x5A5A5A5A
HANDSHAKE_SIG_STANDALONE = 0x12121212

# ClientHandshakeConfig capability bitmask
CFG_VIDEO = 1
CFG_MOUSE = 2
CFG_KEYBOARD = 4
CFG_STORAGE_0 = 8
CFG_STORAGE_1 = 16
CFG_EVERYTHING = 31

# Disconnect reasons (DisconnectReasons)
DISCONNECT_EXIT_SUCCESS = 0
DISCONNECT_FORCE_QUIT = 1
DISCONNECT_EXIT_EXCEPTION = 2

# BiltType (video sub-type)
BLT_COPY = 256
BLT_TEXT_ASCII = 257
BLT_TEXT_ATTRIBUTE = 258
BLT_TEXT_ASCII_ATTRIBUTE = 259
BLT_TEXT_FONT = 260
BLT_TEXT_ASCII_FONT = 261
BLT_TEXT_ATTRIBUTE_FONT = 262
BLT_TEXT_ASCII_ATTRIBUTE_FONT = 263
BLT_TEXT_ASCII_ATTRIBUTE_COMBO = 264
BLT_ENHANCE = 496
BLT_ENHANCE_NEW_FRAME = -32272
BLT_ENHANCE_HLC = 498
BLT_ENHANCE_HLC_NEW_FRAME = -32270
BLT_SPECIAL_4BPP = 499
BLT_SPECIAL_4BPP_NEW_FRAME = -32269

TEXT_BLIT_TYPES = frozenset(
    (
        BLT_TEXT_ASCII,
        BLT_TEXT_ATTRIBUTE,
        BLT_TEXT_ASCII_ATTRIBUTE,
        BLT_TEXT_FONT,
        BLT_TEXT_ASCII_FONT,
        BLT_TEXT_ATTRIBUTE_FONT,
        BLT_TEXT_ASCII_ATTRIBUTE_FONT,
        BLT_TEXT_ASCII_ATTRIBUTE_COMBO,
    )
)

# Media types (DriveType / "media.type.N" strings) -- there's no fixed enum
# for these, just data-driven resource-bundle strings, so the numbering
# below is transcribed directly from the observed values.
MEDIA_TYPE_NOT_SELECTED = 0
MEDIA_TYPE_FLOPPY = 1
MEDIA_TYPE_FLOPPY_RO = 2
MEDIA_TYPE_CDROM = 3
MEDIA_TYPE_DVDROM = 4
MEDIA_TYPE_HARD_DRIVE = 5
MEDIA_TYPE_HARD_DRIVE_RO = 6
MEDIA_TYPE_USB_REMOTE = 7
MEDIA_TYPE_USB_REMOTE_RO = 8
MEDIA_TYPE_FLOPPY_IMAGE = 9
MEDIA_TYPE_FLOPPY_IMAGE_RO = 10
MEDIA_TYPE_CD_ISO_IMAGE = 11
MEDIA_TYPE_DVD_ISO_IMAGE = 12
MEDIA_TYPE_HARD_DRIVE_IMAGE = 13
MEDIA_TYPE_HARD_DRIVE_IMAGE_RO = 14
MEDIA_TYPE_NETWORK_SHARE = 15
MEDIA_TYPE_RAM_DISK = 16
MEDIA_TYPE_RAM_DISK_RO = 17

# StorageClientConnect.UNDEFINED_SHARE / StorageStatus share index "none"
UNDEFINED_SHARE = -1

IP_TYPE_UNKNOWN = 0
IP_TYPE_IPV4 = 1
IP_TYPE_IPV6 = 2

# StorageStatus share status values (storage.status.N resource strings)
STORAGE_STATUS_CONNECTED = 0
STORAGE_STATUS_DISCONNECTED_BY_REQUEST = 1
STORAGE_STATUS_CONNECT_FAILED = 2
STORAGE_STATUS_DISCONNECT_ERROR = 3
STORAGE_STATUS_NOT_CONNECTED = 4

# both StorageClientConnect and StorageStatus pad each path to this
_SHARE_PATH_FIELD_SIZE = 512


def _pad(s: str, n: int) -> str:
    """Truncate/null-pad a string to n chars (see docs/PROTOCOL_ILO100.md)."""
    if len(s) >= n:
        return s[:n]
    return s + "\x00" * (n - len(s))


# -- client -> server encoders ------------------------------------------------


def encode_client_nop() -> bytes:
    """8 bytes total: the id byte + a 7-byte all-zero payload.

    Confirmed against a live capture — ``ClientNOP`` is *not* a bare 1-byte
    message (see docs/PROTOCOL_ILO100.md's "ClientNOP" note): it always
    carries a fixed 7-byte all-zero payload. Getting this wrong desyncs
    every byte sent afterward — exactly the silent-hang failure mode this
    was debugged from.
    """
    return bytes([MSG_CLIENT_NOP]) + b"\x00" * 7


def encode_client_handshake(
    username: str,
    password: str,
    config_mask: int = CFG_EVERYTHING,
    key: str = "",
    standalone: bool = False,
) -> bytes:
    """The auth handshake: username/password sent directly, fixed-length fields.

    Matches the wire format's padding: username to 16 chars, password to 20,
    plus a 128-char "full password" copy of the same password (its purpose
    beyond supporting longer passwords than the 20-char field isn't confirmed
    — see docs/PROTOCOL_ILO100.md). Defaults (``standalone=False``,
    ``config_mask=CFG_EVERYTHING``) match a live capture of the iLO's own
    web console byte-for-byte — it always requests every privilege
    regardless of what it actually uses, and keeps the default embedded
    signature.
    """
    user16 = _pad(username, 16)
    pass20 = _pad(password, 20)
    pass_full128 = _pad(password, 128)
    buf = LEBuffer()
    buf.write_int(HANDSHAKE_SIG_STANDALONE if standalone else HANDSHAKE_SIG_EMBEDDED)
    buf.write_int(len(user16))
    buf.write_int(len(pass20))
    buf.write_int(4)  # ClientHandshakeConfig.length() is always 4
    buf.write_int(len(key))
    buf.write_bytes(user16.encode("latin-1"))
    buf.write_bytes(pass20.encode("latin-1"))
    buf.write_int(config_mask)
    buf.write_bytes(key.encode("latin-1"))
    buf.write_bytes(pass_full128.encode("latin-1"))
    return bytes([MSG_CLIENT_HANDSHAKE]) + buf.bytes()


def encode_client_absolute_mode(enabled: bool) -> bytes:
    """Encode ``ClientAbsoluteMode``: tell the server we'll send absolute
    mouse coordinates."""
    return bytes([MSG_CLIENT_ABSOLUTE_MODE, 1 if enabled else 0])


def encode_client_relative_mode(relative: bool, hide: bool) -> bytes:
    """Encode ``ClientRelativeMode``: switch to relative mouse coordinates,
    optionally hiding the cursor."""
    return bytes([MSG_CLIENT_RELATIVE_MODE, 1 if relative else 0, 1 if hide else 0])


def _encode_button_states(left: bool, right: bool, middle: bool) -> bytes:
    """3 ButtonState bytes: bit0=pressed, bits1-7=trackwheel offset (centered=64)."""
    out = bytearray()
    for pressed in (left, right, middle):
        b = 0x80  # TRACKWHEEL_CENTERED default (matches ButtonState.getInstance())
        if pressed:
            b |= 1
        out.append(b & 0xFF)
    return bytes(out)


def encode_button_state_at_absolute(
    x: int, y: int, left: bool, right: bool, middle: bool
) -> bytes:
    """Encode ``ButtonStateAtAbsolute``: mouse position + button state at
    absolute (x, y)."""
    buf = LEBuffer()
    buf.write_int(x)
    buf.write_int(y)
    buf.write_byte(3)
    buf.write_bytes(_encode_button_states(left, right, middle))
    return bytes([MSG_BUTTON_STATE_AT_ABSOLUTE]) + buf.bytes()


def encode_mouse_move(x: int, y: int) -> bytes:
    """Encode ``MouseMove``: a relative-mode mouse move to (x, y)."""
    buf = LEBuffer()
    buf.write_int(x)
    buf.write_int(y)
    return bytes([MSG_MOUSE_MOVE]) + buf.bytes()


def encode_key_state_change(
    hid_code: int, down: bool, pre_translated: bool = False
) -> bytes:
    """Encode ``KeyStateChange``: a single HID key going down or up."""
    buf = LEBuffer()
    buf.write_short(hid_code)
    buf.write_bool(down)
    buf.write_bool(pre_translated)
    return bytes([MSG_KEY_STATE_CHANGE]) + buf.bytes()


def encode_request_vesa_mode() -> bytes:
    """Encode ``RequestVesaMode``: ask the server to (re)send an ``InformVesaMode``."""
    return bytes([MSG_REQUEST_VESA_MODE])


def encode_request_primary_control() -> bytes:
    """Encode ``RequestPrimaryControl``: ask the server to grant this
    client full input control."""
    return bytes([MSG_REQUEST_PRIMARY_CONTROL])


def encode_relinquish_full_control() -> bytes:
    """Encode ``RelinquishFullControl``: give up full control, dropping
    back to view-only."""
    return bytes([MSG_RELINQUISH_FULL_CONTROL])


def encode_request_key_indicators() -> bytes:
    """Encode ``RequestKeyIndicators``: ask the server for the
    caps/num/scroll lock state."""
    return bytes([MSG_REQUEST_KEY_INDICATORS])


def encode_inform_hlevel_compression(encrypted: bool) -> bytes:
    """Encode ``InformHLevelCompression``: tell the server whether the
    link is encrypted."""
    return bytes([MSG_INFORM_HLEVEL_COMPRESSION, 1 if encrypted else 0])


def encode_client_disconnect(reason: int, message: str = "") -> bytes:
    """Encode ``ClientDisconnect``: tell the server we're closing, with a
    reason code and message."""
    buf = LEBuffer()
    buf.write_int(reason)
    data = message.encode("latin-1")
    buf.write_short(len(data))
    buf.write_bytes(data)
    return bytes([MSG_CLIENT_DISCONNECT]) + buf.bytes()


def encode_invalidate(regions: list[tuple]) -> bytes:
    """``regions``: list of (x, y, w, h) int16 tuples. Empty list is valid."""
    buf = LEBuffer()
    buf.write_short(len(regions))
    for x, y, w, h in regions:
        buf.write_short(x)
        buf.write_short(y)
        buf.write_short(w)
        buf.write_short(h)
    return bytes([MSG_INVALIDATE]) + buf.bytes()


def _signed16(port: int) -> int:
    """LEBuffer.write_short packs a signed ``<h``; a listener bound to an
    ephemeral port (32768-65535, the common case on Linux) overflows that
    unless converted first -- the bytes are what matter on the wire, not
    the sign, since the field itself is wire-defined as a signed 16-bit
    value."""
    return port - 0x10000 if port >= 0x8000 else port


def encode_storage_client_connect(
    ip_address: bytes,
    port: int,
    share_index0: int,
    share_type0: int,
    share_path0: str = "",
    *,
    share_index1: int = UNDEFINED_SHARE,
    share_type1: int = UNDEFINED_SHARE,
    share_path1: str = "",
    ip_type: int = IP_TYPE_IPV4,
) -> bytes:
    """Tell the iLO to dial *us* (``ip_address``:``port``) for the storage
    data channel. ``ip_address`` is right-aligned into a 16-byte field -- a
    plain 4-byte IPv4 address occupies bytes 12-15, bytes 0-11 stay zero.

    This is only the "here's where to connect, and what kind of drive"
    message. What the iLO's firmware actually sends once it dials in is
    handled by a native helper binary (``LIBM2-32/64.SO``/``M2-32/64.DLL``)
    on the iLO side; that data-tunnel wire format is documented separately
    in docs/PROTOCOL_ILO100.md's "Virtual media" section and implemented in
    ``vmedia100.py``.
    """
    buf = LEBuffer()
    ip_field = bytearray(16)
    ip_field[16 - len(ip_address):] = ip_address
    buf.write_bytes(bytes(ip_field))
    buf.write_byte(share_index0)
    buf.write_byte(share_index1)
    buf.write_short(_signed16(port))
    path0 = share_path0.encode("utf-16-le")
    path1 = share_path1.encode("utf-16-le")
    # length in *characters*, not bytes -- matches (byte)String.length()
    buf.write_byte(len(share_path0))
    buf.write_byte(len(share_path1))
    buf.write_byte(share_type0)
    buf.write_byte(share_type1)
    buf.write_byte(ip_type)
    buf.write_bytes(bytes(7))  # reserved
    buf.write_bytes(path0.ljust(_SHARE_PATH_FIELD_SIZE, b"\x00"))
    buf.write_bytes(path1.ljust(_SHARE_PATH_FIELD_SIZE, b"\x00"))
    return bytes([MSG_STORAGE_CLIENT_CONNECT]) + buf.bytes()


def encode_storage_client_disconnect() -> bytes:
    """Encode ``StorageClientDisconnect``: tell the server to tear down
    the virtual media channel."""
    return bytes([MSG_STORAGE_CLIENT_DISCONNECT, 0])


# -- server -> client decoders ------------------------------------------------


@dataclass
class ServerHandshake:
    """Decoded ``ServerHandshake``: which privileges (keyboard/video/mouse/storage)
    the session was granted, plus the raw text it was parsed from."""

    keyboard: bool
    video: bool
    mouse: bool
    storage: bool
    raw: str


@dataclass
class InformVesaMode:
    """Decoded ``InformVesaMode``: the server-selected text/graphics mode
    and resolution."""

    mode_number: int
    width: int
    height: int
    bpp: int


@dataclass
class Region:
    """A rectangular (x, y, width, height) area of the screen."""

    x: int
    y: int
    width: int
    height: int


@dataclass
class BitBlt:
    """Decoded ``BitBlt``: a raw or text-mode block copy from ``source`` to ``dest``."""

    blit_type: int
    font_height: int
    font_width: int
    source: Region
    dest: Region
    data: bytes


@dataclass
class EnhanceBitBlt:
    """Decoded ``EnhanceBitBlt``: a tiled, dirty-rect-tracked, possibly
    compressed screen update."""

    blit_type: int
    tile_width: int
    tile_height: int
    triplet_code: int
    repeat_code: int
    raw_size: int
    scrunch_size: int
    snoop_low: list[int]
    snoop_high: list[int]
    data: bytes


@dataclass
class SetPalette:
    """Decoded ``SetPalette``: RGB palette entries and attribute-table
    entries to merge in."""

    palette: list[tuple]  # (offset, red, green, blue)
    attributes: list[tuple]  # (index, value)


@dataclass
class SetTextCursor:
    """Decoded ``SetTextCursor``: text-mode cursor cell position and scanline shape."""

    x: int
    y: int
    start_line: int
    stop_line: int


@dataclass
class SpecialGraphicsBit:
    """Decoded ``SpecialGraphicsBit``: blink/DBE-swap flags and
    font/color-index offsets."""

    blinking_text: bool
    is_swapping: bool
    primary_select_offset: int
    secondary_select_offset: int
    bpp4_color_index: int


@dataclass
class ServerDisconnect:
    """Decoded ``ServerDisconnect``: why the server is closing the connection."""

    reason: int
    message: str


@dataclass
class StorageStatus:
    """Decoded ``StorageStatus``: current virtual-media share connections
    and their state."""

    # 16-byte field; a plain IPv4 address is right-aligned (bytes 12-15)
    ip_address: bytes
    share_index0: int
    share_index1: int
    port: int
    share_type0: int
    share_type1: int
    share_status0: int
    share_status1: int
    ip_type: int
    share_path0: str
    share_path1: str


@dataclass
class MultiUserState:
    """Decoded ``MultiUserState``: control/storage permission flags for this session."""

    flags: int

    @property
    def user_status(self) -> int:
        """0=full control, 1=view-only, 2=ephemeral, 3=forcible-disconnect
        (see docs/PROTOCOL_ILO100.md)."""
        return (self.flags & 0x18) >> 3

    @property
    def allowed_to_take_full_control(self) -> bool:
        """Whether this client is permitted to request full input control."""
        return bool(self.flags & 1)

    @property
    def has_full_control(self) -> bool:
        """Whether this client currently holds full input control."""
        return self.user_status == 0

    @property
    def allowed_to_do_storage(self) -> bool:
        """``isAllowedToDoStorage`` -- overwrites ``ServerHandshake.storage``
        as the *effective* value the real client uses to gate
        ``StorageClientConnect``: this flag, not the initial handshake
        value, is what actually gates it (see docs/PROTOCOL_ILO100.md).
        Observed live staying false even with an "LO100 Advanced" license
        installed -- what actually flips it true is not yet confirmed."""
        return bool(self.flags & 0x100)


def _read_region(r: SocketReader) -> Region:
    return Region(r.read_i16(), r.read_i16(), r.read_i16(), r.read_i16())


def _is_java_identifier_part(b: int) -> bool:
    # Approximates Character.isJavaIdentifierPart for the ASCII range this
    # 32-byte buffer actually contains (letters/digits/'_'/'$').
    c = chr(b)
    return c.isalnum() or c in ("_", "$")


def decode_server_handshake(r: SocketReader) -> ServerHandshake:
    """Decode the fixed 32-byte ``ServerHandshake`` text and parse its
    k/v/m/s privilege flags."""
    raw_bytes = r.read_bytes(32)
    text = "".join(
        chr(b) if _is_java_identifier_part(b) else " " for b in raw_bytes
    ).strip()
    sp = text.find(" ")
    flags = text[sp + 1 :].strip().lower() if sp != -1 else ""
    return ServerHandshake(
        keyboard="k" in flags,
        video="v" in flags,
        mouse="m" in flags,
        storage="s" in flags,
        raw=text,
    )


def decode_inform_vesa_mode(r: SocketReader) -> InformVesaMode:
    """Decode ``InformVesaMode``: the new mode number, width, height, and bpp."""
    return InformVesaMode(r.read_i16(), r.read_i16(), r.read_i16(), r.read_i16())


def decode_bit_blt(r: SocketReader) -> BitBlt:
    """Decode a ``BitBlt`` message: blit type, font size, source/dest
    rects, and pixel data."""
    blit_type = r.read_i16()
    font_height = r.read_u8()
    font_width = r.read_u8()
    source = _read_region(r)
    dest = _read_region(r)
    size = r.read_i32()
    data = r.read_bytes(size)
    return BitBlt(blit_type, font_height, font_width, source, dest, data)


def decode_enhance_bit_blt(r: SocketReader) -> EnhanceBitBlt:
    """Decode an ``EnhanceBitBlt`` message: tile geometry, RLE escape codes, the
    64x64 dirty-tile snoop bitmaps, and the (possibly compressed) tile data."""
    blit_type = r.read_i16()
    tile_width = r.read_u8()
    tile_height = r.read_u8()
    triplet_code = r.read_i32()
    repeat_code = r.read_i32()
    raw_size = r.read_i32()
    scrunch_size = r.read_i32()
    snoop_low = [0] * 64
    snoop_high = [0] * 64
    for i in range(64):
        snoop_low[i] = r.read_i32()
        snoop_high[i] = r.read_i32()
    data = r.read_bytes(scrunch_size)
    return EnhanceBitBlt(
        blit_type, tile_width, tile_height, triplet_code, repeat_code,
        raw_size, scrunch_size, snoop_low, snoop_high, data,
    )


def decode_set_palette(r: SocketReader) -> SetPalette:
    """Decode ``SetPalette``: variable-length attribute-table entries
    followed by RGB palette entries."""
    attr_size = r.read_i16()
    attrs = [(r.read_u8(), r.read_u8()) for _ in range(attr_size)]
    pal_size = r.read_i16()
    palette = []
    for _ in range(pal_size):
        offset = r.read_u8()
        hi = r.read_u8()  # red
        mid = r.read_u8()  # green
        lo = r.read_u8()  # blue
        palette.append((offset, hi, mid, lo))
    return SetPalette(palette=palette, attributes=attrs)


def decode_set_text_cursor(r: SocketReader) -> SetTextCursor:
    """Decode ``SetTextCursor``, normalizing start/stop scanlines so start <= stop."""
    x, y, start, stop = r.read_u8(), r.read_u8(), r.read_u8(), r.read_u8()
    if stop < start:
        start, stop = stop, start
    return SetTextCursor(x, y, start, stop)


_CHARACTER_MAP_OFFSET = (0, 512, 1024, 1536, 256, 768, 1280, 1792)


def decode_special_graphics_bit(r: SocketReader) -> SpecialGraphicsBit:
    """Decode ``SpecialGraphicsBit``'s packed 32-bit flags into
    blink/swap/offset/color-index fields."""
    v = r.read_i32()
    primary = (v >> 2) & 7
    secondary = (v >> 5) & 7
    return SpecialGraphicsBit(
        blinking_text=(v & 1) != 0,
        is_swapping=((v >> 1) & 1) != 0,
        primary_select_offset=_CHARACTER_MAP_OFFSET[primary],
        secondary_select_offset=_CHARACTER_MAP_OFFSET[secondary],
        bpp4_color_index=(v >> 8) & 3,
    )


def decode_matrox_graphics_cursor(r: SocketReader) -> None:
    """Consume a ``MatroxGraphicsCursor`` message's bytes without acting
    on them (hardware cursor overlay, not rendered)."""
    r.read_bytes(256)  # control
    for _ in range(64):
        r.read_bytes(48)  # data rows


def decode_standby_power(r: SocketReader) -> None:
    """Consume a ``StandbyPower`` message's bytes without acting on them."""
    r.read_bytes(15)


def decode_inform_key_indicators(r: SocketReader) -> tuple:
    """Decode ``InformKeyIndicators`` into a (caps, num, scroll) bool tuple."""
    return (r.read_bool(), r.read_bool(), r.read_bool())  # caps, num, scroll


def decode_server_disconnect(r: SocketReader) -> ServerDisconnect:
    """Decode ``ServerDisconnect``: the reason code and an optional
    human-readable message."""
    reason = r.read_i32()
    n = r.read_i16()
    message = r.read_bytes(n).decode("latin-1")
    return ServerDisconnect(reason, message)


def decode_firmware_version(r: SocketReader) -> str:
    """Decode a length-prefixed ``FirmwareVersion`` string."""
    n = r.read_i32()
    return r.read_bytes(n).decode("latin-1")


def decode_multi_user_state(r: SocketReader) -> MultiUserState:
    """Decode ``MultiUserState``, discarding the per-user name/flag
    fields we don't use."""
    flags = r.read_i32()
    name_size0 = r.read_u8()
    name_size1 = r.read_u8()
    r.read_u8()  # user0 flags
    r.read_u8()  # user1 flags
    r.read_bytes(name_size0)
    r.read_bytes(name_size1)
    return MultiUserState(flags=flags)


def decode_storage_status(r: SocketReader) -> StorageStatus:
    """Decode ``StorageStatus``: connection state and share paths for
    both storage slots."""
    ip_address = r.read_bytes(16)
    share_index0 = r.read_u8()
    share_index1 = r.read_u8()
    port = r.read_u16()
    name_len0 = r.read_u8()
    name_len1 = r.read_u8()
    share_type0 = r.read_u8()
    share_type1 = r.read_u8()
    share_status0 = r.read_u8()
    share_status1 = r.read_u8()
    ip_type = r.read_u8()
    r.read_bytes(5)  # reserved
    # Each path is a fixed 512-byte UTF-16LE buffer; name_len is a character
    # count (StorageStatus.readBuffer: `new String(byArray, 0, s << 1, ...)`),
    # so the actual byte slice is twice that.
    path0_buf = r.read_bytes(_SHARE_PATH_FIELD_SIZE)
    path1_buf = r.read_bytes(_SHARE_PATH_FIELD_SIZE)
    share_path0 = path0_buf[: name_len0 * 2].decode("utf-16-le", errors="replace")
    share_path1 = path1_buf[: name_len1 * 2].decode("utf-16-le", errors="replace")
    return StorageStatus(
        ip_address=ip_address,
        share_index0=share_index0,
        share_index1=share_index1,
        port=port,
        share_type0=share_type0,
        share_type1=share_type1,
        share_status0=share_status0,
        share_status1=share_status1,
        ip_type=ip_type,
        share_path0=share_path0,
        share_path1=share_path1,
    )


def decode_graphics_register_value(r: SocketReader) -> None:
    """Consume a ``GraphicsRegisterValue`` message's fields without acting on them."""
    for _ in range(5 + 8 + 2 + 2):
        r.read_i32()
