"""Tests for the iLO 100 video decoder (text mode + graphics BitBlt codecs)."""

from ilo100console import protocol as p
from ilo100console.video import VideoState


def test_set_vesa_mode_text_geometry():
    vs = VideoState()
    vs.set_vesa_mode(0, 8, 0, 0)  # mode 0 -> 80x25 text, 8x16 font cells
    assert vs.mode == "text"
    assert (vs.width, vs.height) == (640, 400)
    assert (vs.text_cols, vs.text_rows) == (80, 25)


def test_set_vesa_mode_graphics_geometry():
    vs = VideoState()
    vs.set_vesa_mode(999, 16, 1024, 768)
    assert vs.mode == "graphics"
    assert (vs.width, vs.height, vs.bpp) == (1024, 768, 16)


def test_text_mode_glyph_renders_through_palette():
    vs = VideoState()
    vs.set_vesa_mode(0, 8, 0, 0)
    vs.font_table[65 * 32 + 0] = 0b11111111  # glyph 'A' row 0: all pixels on
    data = bytes([65, 0x0F])  # ascii='A', attribute: fg=15, bg=0
    msg = p.BitBlt(
        blit_type=p.BLT_TEXT_ASCII_ATTRIBUTE,
        font_height=16, font_width=8,
        source=p.Region(0, 0, 1, 1), dest=p.Region(0, 0, 1, 1), data=data,
    )
    rect = vs.process_bit_blt(msg)
    assert rect == (0, 0, 8, 16)
    rgb = vs.render_rgb()
    assert tuple(rgb[0:3]) == (vs.pal_r[15], vs.pal_g[15], vs.pal_b[15])
    # Second glyph cell (untouched) stays background color (palette index 0).
    assert tuple(rgb[24:27]) == (vs.pal_r[0], vs.pal_g[0], vs.pal_b[0])


def test_text_mode_multiple_cells_advance_horizontally():
    # Regression test: an earlier version reset the pixel cursor to the start
    # of the scanline on every character cell instead of accumulating across
    # the row, so every character overwrote the same first font_width pixels
    # and the rest of each text row stayed blank. A single-cell test can't
    # catch this — it takes 2+ cells in one row to see cell 1 clobber cell 0.
    vs = VideoState()
    vs.set_vesa_mode(0, 8, 0, 0)
    vs.font_table[65 * 32 + 0] = 0b11111111  # 'A': row 0 fully lit
    vs.font_table[66 * 32 + 0] = 0b00000000  # 'B': row 0 fully dark
    # Wire layout is two sequential blocks (all ascii bytes, then all
    # attribute bytes) — NOT interleaved per character.
    data = bytes([65, 66]) + bytes([0x0F, 0x0F])  # "AB", both fg=15 bg=0
    msg = p.BitBlt(
        blit_type=p.BLT_TEXT_ASCII_ATTRIBUTE,
        font_height=16, font_width=8,
        source=p.Region(0, 0, 2, 1), dest=p.Region(0, 0, 2, 1), data=data,
    )
    vs.process_bit_blt(msg)
    rgb = vs.render_rgb()
    fg = (vs.pal_r[15], vs.pal_g[15], vs.pal_b[15])
    bg = (vs.pal_r[0], vs.pal_g[0], vs.pal_b[0])
    # Cell 0 ('A', all-lit row): pixels 0..7 must all be foreground.
    for px in range(8):
        assert tuple(rgb[px * 3 : px * 3 + 3]) == fg, f"cell 0 pixel {px}"
    # Cell 1 ('B', all-dark row) starts at pixel 8, not pixel 0 — this is
    # what the bug broke: cell 1 used to overwrite pixels 0..7 again.
    for px in range(8, 16):
        assert tuple(rgb[px * 3 : px * 3 + 3]) == bg, f"cell 1 pixel {px}"


def test_text_mode_font_component_updates_font_table():
    vs = VideoState()
    vs.set_vesa_mode(0, 8, 0, 0)
    font_blob = bytearray(65536)
    font_blob[65 * 32] = 0xFF
    data = bytes([65, 0x0F]) + bytes(font_blob)
    msg = p.BitBlt(
        blit_type=p.BLT_TEXT_ASCII_ATTRIBUTE_FONT,
        font_height=16, font_width=8,
        source=p.Region(0, 0, 1, 1), dest=p.Region(0, 0, 1, 1), data=data,
    )
    vs.process_bit_blt(msg)
    assert vs.font_table[65 * 32] == 0xFF
    rgb = vs.render_rgb()
    assert tuple(rgb[0:3]) == (vs.pal_r[15], vs.pal_g[15], vs.pal_b[15])


def test_graphics_bitblt_raw_copy_16bpp():
    vs = VideoState()
    vs.set_vesa_mode(999, 16, 2, 1)
    # pixel0: blue=0xAB, green=0xCD -> px=0xCDAB; pixel1: blue=0x01,green=0x00
    data = bytes([0xAB, 0xCD, 0x01, 0x00])
    msg = p.BitBlt(
        blit_type=p.BLT_COPY, font_height=0, font_width=0,
        source=p.Region(0, 0, 2, 1), dest=p.Region(0, 0, 2, 1), data=data,
    )
    vs.process_bit_blt(msg)
    assert vs.pixel_screen[0] == 0xCDAB
    assert vs.pixel_screen[1] == 0x0001


def test_enhance_bit_blt_raw_dirty_tile():
    vs = VideoState()
    vs.set_vesa_mode(999, 16, 32, 32)
    snoop_low = [0] * 64
    snoop_low[0] = 1  # tile (col0,row0) dirty
    tile_bytes = bytes([0xAB, 0xCD] * (16 * 16))
    msg = p.EnhanceBitBlt(
        blit_type=p.BLT_ENHANCE, tile_width=16, tile_height=16,
        triplet_code=0, repeat_code=0, raw_size=0, scrunch_size=len(tile_bytes),
        snoop_low=snoop_low, snoop_high=[0] * 64, data=tile_bytes,
    )
    rect = vs.process_enhance_bit_blt(msg)
    assert rect == (0, 0, 16, 16)
    assert vs.pixel_screen[0] == 0xCDAB
    # An untouched tile elsewhere stays zero.
    assert vs.pixel_screen[20 * vs.width + 20] == 0


def test_enhance_bit_blt_hlc_triplet_and_literal():
    vs = VideoState()
    vs.set_vesa_mode(999, 8, 4, 4)
    snoop_low = [0] * 64
    snoop_low[0] = 1
    triplet, repeat = 0xFE, 0xFF
    # literal 5, then a triplet-escape of value 7 (repeats 3x) -> 4 px total.
    data = bytes([5, triplet, 7])
    msg = p.EnhanceBitBlt(
        blit_type=p.BLT_ENHANCE_HLC, tile_width=2, tile_height=2,
        triplet_code=triplet, repeat_code=repeat, raw_size=0, scrunch_size=len(data),
        snoop_low=snoop_low, snoop_high=[0] * 64, data=data,
    )
    vs.process_enhance_bit_blt_hlc(msg)
    assert list(vs.palette_screen[0:2]) == [5, 7]
    assert list(vs.palette_screen[vs.width : vs.width + 2]) == [7, 7]


def test_enhance_bit_blt_hlc_repeat_count():
    vs = VideoState()
    vs.set_vesa_mode(999, 8, 4, 4)
    snoop_low = [0] * 64
    snoop_low[0] = 1
    triplet, repeat = 0xFE, 0xFF
    # repeat-escape with count=3 -> run of 4 pixels of value 9.
    data = bytes([repeat, 3, 9])
    msg = p.EnhanceBitBlt(
        blit_type=p.BLT_ENHANCE_HLC, tile_width=2, tile_height=2,
        triplet_code=triplet, repeat_code=repeat, raw_size=0, scrunch_size=len(data),
        snoop_low=snoop_low, snoop_high=[0] * 64, data=data,
    )
    vs.process_enhance_bit_blt_hlc(msg)
    assert all(v == 9 for v in vs.palette_screen[0:2])
    assert all(v == 9 for v in vs.palette_screen[vs.width : vs.width + 2])


def test_enhance_bit_blt_hlc_repeat_count_escapes_literal_control_bytes():
    vs = VideoState()
    vs.set_vesa_mode(999, 8, 2, 1)
    triplet, repeat = 0xFE, 0xFF
    snoop_low = [0] * 64
    snoop_low[0] = 1
    # count==1 -> literal value IS the triplet-code byte itself.
    # count==0 -> literal value IS the repeat-code byte itself.
    data = bytes([repeat, 1]) + bytes([repeat, 0])
    msg = p.EnhanceBitBlt(
        blit_type=p.BLT_ENHANCE_HLC, tile_width=2, tile_height=1,
        triplet_code=triplet, repeat_code=repeat, raw_size=0, scrunch_size=len(data),
        snoop_low=snoop_low, snoop_high=[0] * 64, data=data,
    )
    vs.process_enhance_bit_blt_hlc(msg)
    assert list(vs.palette_screen[0:2]) == [triplet, repeat]


def test_convert_attributes_to_palette_identity_default():
    vs = VideoState()
    vs.set_vesa_mode(999, 8, 2, 1)
    vs.attribute_screen[0] = 7
    vs.convert_attributes_to_palette(0, 0, 2, 1)
    assert vs.palette_screen[0] == 7  # identity attribute table by default


def test_render_rgb_empty_before_any_mode_set():
    vs = VideoState()
    assert vs.render_rgb() == b""
