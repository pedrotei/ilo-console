"""Byte-exact tests for input frame encoders vs. docs/PROTOCOL_ILO3.md."""

from ilo3console import input as inp


def test_keyboard_report_layout():
    frame = inp.keyboard_report(inp.MOD_LCTRL | inp.MOD_LALT, [inp.HID_DELETE])
    assert frame == bytes([1, 0, 0x05, 0, 0x4C, 0, 0, 0, 0, 0])


def test_keyboard_report_caps_at_six_keys():
    frame = inp.keyboard_report(0, range(1, 10))
    assert frame == bytes([1, 0, 0, 0, 1, 2, 3, 4, 5, 6])


def test_ctrl_alt_del_press_then_release():
    press, release = inp.ctrl_alt_del()
    assert press == bytes([1, 0, 0x05, 0, 0x4C, 0, 0, 0, 0, 0])
    # The all-keys-up report keeps the 0x01 record-type byte
    # (see docs/PROTOCOL_ILO3.md).
    assert release == bytes([1, 0, 0, 0, 0, 0, 0, 0, 0, 0])


def test_function_keys():
    # Ctrl-Alt-F1 → mod 0x05, HID 0x3A.
    assert inp.ctrl_alt_fn(0)[0] == bytes([1, 0, 0x05, 0, 0x3A, 0, 0, 0, 0, 0])
    # Alt-F12 → mod 0x04, HID 0x45.
    assert inp.alt_fn(11)[0] == bytes([1, 0, 0x04, 0, 0x45, 0, 0, 0, 0, 0])


def test_power_records():
    assert inp.power(inp.POWER_PULSE) == bytes([0, 0, 0, 0])
    assert inp.power(inp.POWER_HOLD) == bytes([0, 0, 1, 0])
    assert inp.power(inp.POWER_CYCLE) == bytes([0, 0, 2, 0])
    assert inp.power(inp.POWER_RESET) == bytes([0, 0, 3, 0])


def test_mouse_absolute_scaling_and_layout():
    # Middle of a 1024x768 screen → 1500,1500 on the 0..3000 wire scale.
    frame = inp.mouse_absolute(512, 384, 1024, 768, dx=3, dy=-4, buttons=inp.MOUSE_LEFT)
    assert frame[0] == inp.REC_MOUSE
    wx = frame[2] | (frame[3] << 8)
    wy = frame[4] | (frame[5] << 8)
    assert wx == 1500 and wy == 1500
    assert frame[6] == 3
    assert frame[7] == (256 - 4)  # -4 as unsigned byte
    assert frame[8] == inp.MOUSE_LEFT


def test_mouse_delta_clamped():
    frame = inp.mouse_absolute(0, 0, 1024, 768, dx=999, dy=-999)
    assert frame[6] == 127
    assert frame[7] == (256 - 127)


def test_refresh_and_ack():
    assert inp.refresh_screen() == bytes([0x05, 0x00])
    assert inp.ack() == bytes([0x0C, 0x00])
