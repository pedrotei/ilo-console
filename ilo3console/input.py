"""Encoders for client -> server input records (keyboard, mouse, power).

Every record is a short binary frame whose first byte is the record type. These
go out on the console channel and are XOR-encrypted by the transport when
encryption is active (see :mod:`ilo3console.console`), so this module only builds
the cleartext frames.

See docs/PROTOCOL_ILO3.md's "Input protocol" section for the byte-exact
frame layouts.
"""

from __future__ import annotations

from collections.abc import Iterable

# Record type bytes.
REC_POWER = 0x00
REC_KEYBOARD = 0x01
REC_MOUSE = 0x02
REC_REFRESH = 0x05
REC_ACK = 0x0C

# Power options (byte[2] of a power record).
POWER_PULSE = 0  # momentary press
POWER_HOLD = 1  # press and hold
POWER_CYCLE = 2  # cold boot
POWER_RESET = 3  # system reset

# USB HID modifier bits (byte[2] of a keyboard record).
MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_LGUI = 0x08
MOD_RCTRL = 0x10
MOD_RSHIFT = 0x20
MOD_RALT = 0x40
MOD_RGUI = 0x80

# A few HID usage codes used by the convenience combos below.
HID_DELETE = 0x4C
HID_BACKSPACE = 0x2A
HID_CAPSLOCK = 0x39
HID_NUMLOCK = 0x53
HID_F1 = 0x3A  # F1..F12 are 0x3A..0x45

# Mouse button bits (cim.mouseButtonState) — note the non-obvious ordering.
MOUSE_LEFT = 0x02
MOUSE_CENTER = 0x04
MOUSE_RIGHT = 0x01


def keyboard_report(modifiers: int, keys: Iterable[int] = ()) -> bytes:
    """Build a 10-byte USB-HID keyboard record.

    ``modifiers`` is the HID modifier bitmap (``MOD_*``); ``keys`` are up to six
    HID usage codes currently held. The frame is
    ``[0x01, 0x00, modifiers, 0x00, k0..k5]`` with unused key slots zeroed — the
    same layout ``telnet.handleKey`` emits. An empty report releases all keys.
    """
    keys = list(keys)[:6]
    frame = bytearray(10)
    frame[0] = REC_KEYBOARD
    frame[2] = modifiers & 0xFF
    for i, code in enumerate(keys):
        frame[4 + i] = code & 0xFF
    return bytes(frame)


def keyboard_release() -> bytes:
    """The all-keys-up report."""
    return keyboard_report(0)


def _combo(modifiers: int, key: int) -> list[bytes]:
    """A press-then-release pair for a single modified keystroke."""
    return [keyboard_report(modifiers, [key]), keyboard_release()]


def ctrl_alt_del() -> list[bytes]:
    """Press-then-release frames for Ctrl+Alt+Delete."""
    return _combo(MOD_LCTRL | MOD_LALT, HID_DELETE)


def ctrl_alt_backspace() -> list[bytes]:
    """Press-then-release frames for Ctrl+Alt+Backspace."""
    return _combo(MOD_LCTRL | MOD_LALT, HID_BACKSPACE)


def num_lock() -> list[bytes]:
    """Press-then-release frames for the Num Lock key."""
    return _combo(0, HID_NUMLOCK)


def caps_lock() -> list[bytes]:
    """Press-then-release frames for the Caps Lock key."""
    return _combo(0, HID_CAPSLOCK)


def ctrl_alt_fn(n: int) -> list[bytes]:
    """Ctrl-Alt-F(n+1); ``n`` is 0-based (0 -> F1)."""
    if not 0 <= n < 12:
        raise ValueError("function key index must be 0..11")
    return _combo(MOD_LCTRL | MOD_LALT, HID_F1 + n)


def alt_fn(n: int) -> list[bytes]:
    """Alt-F(n+1); ``n`` is 0-based (0 -> F1)."""
    if not 0 <= n < 12:
        raise ValueError("function key index must be 0..11")
    return _combo(MOD_LALT, HID_F1 + n)


def power(option: int) -> bytes:
    """Build a 4-byte power record. ``option`` is one of ``POWER_*``."""
    if option not in (POWER_PULSE, POWER_HOLD, POWER_CYCLE, POWER_RESET):
        raise ValueError(f"invalid power option: {option}")
    return bytes([REC_POWER, 0x00, option & 0xFF, 0x00])


def mouse_absolute(
    x: int,
    y: int,
    screen_x: int,
    screen_y: int,
    dx: int = 0,
    dy: int = 0,
    buttons: int = 0,
) -> bytes:
    """Build a 10-byte absolute-mouse record.

    ``x``/``y`` are framebuffer pixel coordinates; they are scaled to the wire's
    0..3000 range as ``cim.sendMouse`` does. ``dx``/``dy`` are signed relative
    deltas (clamped to ±127) and ``buttons`` is a ``MOUSE_*`` bitmap.
    """
    wx = (3000 * x // screen_x) if screen_x > 0 else 3000 * x
    wy = (3000 * y // screen_y) if screen_y > 0 else 3000 * y
    wx &= 0xFFFF
    wy &= 0xFFFF
    dx = max(-127, min(127, dx))
    dy = max(-127, min(127, dy))
    return bytes(
        [
            REC_MOUSE,
            0x00,
            wx & 0xFF,
            (wx >> 8) & 0xFF,
            wy & 0xFF,
            (wy >> 8) & 0xFF,
            dx & 0xFF,
            dy & 0xFF,
            buttons & 0xFF,
            0x00,
        ]
    )


def refresh_screen() -> bytes:
    """Request a full screen refresh (2 bytes)."""
    return bytes([REC_REFRESH, 0x00])


def ack() -> bytes:
    """Reply to the DVC ack-request command (2 bytes)."""
    return bytes([REC_ACK, 0x00])
