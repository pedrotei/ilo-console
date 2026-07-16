"""USB HID usage-code table for keyboard input.

The wire scancode in ``KeyStateChange`` is confirmed to be a literal USB HID
usage id (e.g. HID 0x04 is 'a' — see docs/PROTOCOL_ILO100.md). This is the
same standard table iLO 3 uses; duplicated here rather than shared so the
two console packages stay independently maintainable.
"""

from __future__ import annotations

# Modifier bits are sent as ordinary keys here (HID 0xE0..0xE7), not folded
# into a separate byte like iLO 3's keyboard report — Mahogany's
# KeyStateChange is one key at a time.
HID_LCTRL = 0xE0
HID_LSHIFT = 0xE1
HID_LALT = 0xE2
HID_LGUI = 0xE3
HID_RCTRL = 0xE4
HID_RSHIFT = 0xE5
HID_RALT = 0xE6
HID_RGUI = 0xE7

HID_DELETE = 0x4C
HID_BACKSPACE = 0x2A
HID_CAPSLOCK = 0x39
HID_NUMLOCK = 0x53
HID_F1 = 0x3A  # F1..F12 are 0x3A..0x45


def _build_table() -> dict:
    m: dict = {}
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        m["Key" + c.upper()] = 0x04 + i
    for i in range(1, 10):
        m["Digit" + str(i)] = 0x1E + (i - 1)
    m["Digit0"] = 0x27
    m.update(
        {
            "Enter": 0x28, "Escape": 0x29, "Backspace": HID_BACKSPACE, "Tab": 0x2B,
            "Space": 0x2C, "Minus": 0x2D, "Equal": 0x2E, "BracketLeft": 0x2F,
            "BracketRight": 0x30, "Backslash": 0x31, "Semicolon": 0x33,
            "Quote": 0x34, "Backquote": 0x35, "Comma": 0x36, "Period": 0x37,
            "Slash": 0x38, "CapsLock": HID_CAPSLOCK,
            "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,
            "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B, "Delete": HID_DELETE,
            "End": 0x4D, "PageDown": 0x4E, "ArrowRight": 0x4F, "ArrowLeft": 0x50,
            "ArrowDown": 0x51, "ArrowUp": 0x52, "NumLock": HID_NUMLOCK,
            "ControlLeft": HID_LCTRL, "ShiftLeft": HID_LSHIFT, "AltLeft": HID_LALT,
            "MetaLeft": HID_LGUI, "ControlRight": HID_RCTRL, "ShiftRight": HID_RSHIFT,
            "AltRight": HID_RALT, "MetaRight": HID_RGUI,
        }
    )
    for i in range(1, 13):
        m["F" + str(i)] = HID_F1 + (i - 1)
    return m


HID_TABLE = _build_table()


def hid_for_code(browser_code: str) -> int:
    """Map a DOM ``KeyboardEvent.code`` (or equivalent) to a HID usage id.

    Returns ``-1`` for unmapped keys (matching the server's own sentinel for
    "no HID mapping"; such keys are simply not sent).
    """
    return HID_TABLE.get(browser_code, -1)
