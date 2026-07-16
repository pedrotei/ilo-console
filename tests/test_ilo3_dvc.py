"""Unit + smoke tests for the DVC video decoder.

We lack a recorded capture, so these pin the individually-verifiable pieces
(colour remap, bit-reversal, cache, command dispatch) and fuzz the full state
machine to guarantee it never throws or runs away on arbitrary input.
"""

import random

from ilo3console.dvc import DvcController, DvcDecoder, Framebuffer


class Recorder(DvcController):
    """Captures every side-band callback the decoder fires."""

    def __init__(self):
        self.events = []

    def set_cipher(self, cipher):
        self.events.append(("cipher", cipher))

    def send_ack(self):
        self.events.append(("ack",))

    def refresh_screen(self):
        self.events.append(("refresh",))

    def on_power(self, on):
        self.events.append(("power", on))

    def on_licensed(self, flags):
        self.events.append(("licensed", flags))

    def on_flags(self, flags):
        self.events.append(("flags", flags))

    def on_framerate(self, fps):
        self.events.append(("fps", fps))

    def on_resize(self, w, h):
        self.events.append(("resize", w, h))

    def on_frame(self):
        self.events.append(("frame",))

    def on_seize(self):
        self.events.append(("seize",))


def _decoder():
    fb = Framebuffer()
    ctl = Recorder()
    return DvcDecoder(fb, ctl), fb, ctl


def test_framebuffer_paste_and_serialise():
    fb = Framebuffer(32, 32)
    block = [0x112233] * 256
    fb.paste_block(block, 0, 0, 16, 16)
    assert fb.pixels[0] == 0x112233
    assert fb.pixels[16] == 0  # outside the pasted block
    rgb = fb.to_rgb_bytes()
    assert rgb[0:3] == bytes([0x11, 0x22, 0x33])


def test_paste_block_clips_to_bounds():
    fb = Framebuffer(8, 8)
    block = [0xFFFFFF] * 256
    # Placing a 16x16 block at (0,0) into an 8x8 buffer must not overflow.
    fb.paste_block(block, 0, 0, 16, 16)
    assert len(fb.pixels) == 64
    assert all(px == 0xFFFFFF for px in fb.pixels)


def test_pixel_table_5bpc():
    dec, _, _ = _decoder()
    t = dec.color_remap
    assert t[0] == 0x000000
    assert t[0x1F] == 0x0000F8  # 5-bit blue → top 5 bits of the blue byte
    assert t[0x3E0] == 0x00F800  # green
    assert t[0x7C00] == 0xF80000  # red
    assert t[0x7FFF] == 0xF8F8F8  # white


def test_bit_reversal_table():
    dec, _, _ = _decoder()
    assert dec.reversal[1] == 0x80
    assert dec.reversal[0x80] == 1
    assert dec.right[1] == 0 and dec.left[1] == 7
    assert dec.right[0] == 8 and dec.left[0] == 8


def test_cache_insert_and_find():
    dec, _, _ = _decoder()
    # Freshly-seen colour is inserted (returns 0 = "was not present").
    assert dec._cache_lru(0x1234) == 0
    assert dec.cc_active == 1
    # Most-recently-used sits at usage index 0.
    assert dec._cache_find(0) == 0x1234


def test_command_set_cipher():
    dec, _, ctl = _decoder()
    dec.cmd_last = 12
    dec.cmd_count = 1
    dec.cmd_buf[0] = 1  # RC4
    dec._dispatch_command()
    assert ("cipher", 1) in ctl.events


def test_command_header_sets_everything():
    dec, _, ctl = _decoder()
    dec.cmd_last = 13
    dec.cmd_count = 4
    dec.cmd_buf[0:4] = [0, 2, 1, 8]  # bpc sel, AES128, licensed, flags
    dec._dispatch_command()
    assert ("cipher", 2) in ctl.events
    assert ("licensed", 1) in ctl.events
    assert ("flags", 8) in ctl.events


def test_command_power_and_seize():
    dec, fb, ctl = _decoder()
    dec.cmd_last = 4
    dec._dispatch_command()
    dec.cmd_last = 10
    dec._dispatch_command()
    assert ("power", True) in ctl.events
    assert ("seize",) in ctl.events


def test_set_bits_per_color_updates_state():
    dec, _, _ = _decoder()
    dec._set_bits_per_color(1)  # 5 - (1 & 3) = 4 bpc
    assert dec.bits_per_color == 4
    from ilo3console.dvc import PIXGREY

    assert dec.bits_to_read[PIXGREY] == 4


def test_fuzz_never_crashes():
    dec, fb, _ = _decoder()
    rng = random.Random(1234)
    for _ in range(50000):
        dec.feed(rng.getrandbits(8))
    # Framebuffer stays bounded and self-consistent after arbitrary input.
    assert len(fb.pixels) == fb.width * fb.height
