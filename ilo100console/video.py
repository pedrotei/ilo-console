"""Video state machine: VGA text mode + graphics mode (BitBlt/EnhanceBitBlt).

See docs/PROTOCOL_ILO100.md's "Video" section. Two coexisting pixel
representations:

- **bpp <= 8** (VGA text mode, and low-color-depth graphics modes): each pixel
  is an *attribute* index (``attribute_screen``) resolved through a 64-entry
  attribute table into a *palette* index (``palette_screen``), itself resolved
  through a 256-entry RGB palette. This is the classic VGA attribute
  controller -> DAC indirection.
- **bpp > 8**: pixels are direct packed RGB ints (``pixel_screen``), no
  palette involved.

Text mode renders 8x16 (or similar) glyphs from a font bitmap the *server*
sends over the wire (there is no bundled font asset — BIOS text is whatever
font the real hardware's VGA ROM has).
"""

from __future__ import annotations

from . import protocol as p

_CHARACTER_MAP_OFFSET = (0, 512, 1024, 1536, 256, 768, 1280, 1792)

# Text-mode blit-type component bits (TextMode.ASCII/ATTRIBUTE/FONT_BIT_BLT)
_BLT_HAS_ASCII = 1
_BLT_HAS_ATTRIBUTE = 2
_BLT_HAS_FONT = 4


def _default_palette() -> tuple[bytearray, bytearray, bytearray]:
    """PaletteMgr's constructor default (a 3-3-2-ish RGB ramp), before any
    SetPalette message arrives."""
    r, g, b = bytearray(256), bytearray(256), bytearray(256)
    for i in range(256):
        r[i] = ((i & 7) * 255 + 3) // 7
        g[i] = (((i >> 3) & 7) * 255 + 3) // 7
        b[i] = (((i >> 6) & 3) * 255 + 1) // 3
    return r, g, b


class VideoState:
    """Holds the current screen mode, palette, and rendered framebuffer."""

    def __init__(self) -> None:
        self.mode = "graphics"  # or "text"
        self.width = 0
        self.height = 0
        self.bpp = 16
        self.generation = 0  # bumped on any resize, so callers can detect it

        # Text-mode geometry (character cells, not pixels).
        self.text_cols = 0
        self.text_rows = 0
        self.font_width = 8
        self.font_height = 16
        self.blit_type = p.BLT_TEXT_ASCII_ATTRIBUTE_FONT

        self.pal_r, self.pal_g, self.pal_b = _default_palette()
        self.attributes = bytearray(range(64))  # identity default, per TextMode ctor
        self.color_index = 0
        self.is_special_4bpp_mode = False

        # Text-mode tables (sized like the reference's fixed 4000/65536 caps,
        # since cols*rows always fits comfortably under a real BIOS mode).
        self.ascii_table = bytearray(4000)
        self.attribute_table = bytearray(4000)
        self.attribute_table_valid = False
        self.font_table = bytearray(65536)
        self.cursor_x = -1
        self.cursor_y = -1
        self.start_line = 0
        self.stop_line = 0
        self.is_blinking = False
        self.is_dbe_swap = False
        self.primary_offset = 0
        self.secondary_offset = 0

        self.attribute_screen = bytearray(0)
        self.palette_screen = bytearray(0)
        self.pixel_screen: list[int] = []

    # -- mode / geometry --------------------------------------------------

    def set_vesa_mode(
        self, mode_number: int, bpp: int, width: int, height: int
    ) -> None:
        """``InformVesaMode`` — text mode numbers vs. a raw graphics resolution.

        Matches ``ScreenModes.getScreenMode``: mode 0/2/3/7 -> 80x25 text,
        1 -> 40x25 text, 266 (0x10A) -> 132x43 text (all 8x16 font cells);
        anything else is a graphics mode at the given bpp/width/height.
        """
        text_geometries = {
            0: (80, 25), 2: (80, 25), 3: (80, 25), 7: (80, 25),
            1: (40, 25), 266: (132, 43),
        }
        if mode_number in text_geometries:
            cols, rows = text_geometries[mode_number]
            self.mode = "text"
            self.font_width = 8
            self.font_height = 16
            self.text_cols = cols
            self.text_rows = rows
            self.width = cols * self.font_width
            self.height = rows * self.font_height
            self.bpp = 8
            self.cursor_x = -1
            self.cursor_y = -1
            self.attribute_table_valid = False
        else:
            self.mode = "graphics"
            self.width = width
            self.height = height
            self.bpp = 8 if bpp < 8 else (16 if bpp == 15 else bpp)
        self.is_special_4bpp_mode = False
        self._resize_buffers()
        self.generation += 1

    def _resize_buffers(self) -> None:
        n = self.width * self.height
        self.attribute_screen = bytearray(n)
        self.palette_screen = bytearray(n)
        self.pixel_screen = [0] * n

    # -- rendering ------------------------------------------------------------

    def render_rgb(self) -> bytes:
        """Packed RGB888, row-major — resolves the palette for bpp<=8 modes."""
        if self.width == 0 or self.height == 0:
            return b""
        out = bytearray(self.width * self.height * 3)
        if self.bpp > 8:
            for i, px in enumerate(self.pixel_screen):
                out[i * 3] = (px >> 16) & 0xFF
                out[i * 3 + 1] = (px >> 8) & 0xFF
                out[i * 3 + 2] = px & 0xFF
        else:
            # Resolve the whole palette_screen in one C-speed pass per channel
            # (bytes.translate is a 256-entry byte lookup) instead of a
            # per-pixel Python loop, then interleave R/G/B via strided
            # slice-assignment.
            out[0::3] = self.palette_screen.translate(bytes(self.pal_r))
            out[1::3] = self.palette_screen.translate(bytes(self.pal_g))
            out[2::3] = self.palette_screen.translate(bytes(self.pal_b))
        return bytes(out)

    # -- palette / attributes -----------------------------------------------

    def set_palette(self, msg: p.SetPalette) -> None:
        """Apply a ``SetPalette`` update: merge attribute-table and RGB entries,
        then re-resolve attributes to palette indices for bpp<8 modes."""
        for index, value in msg.attributes:
            if index < len(self.attributes):
                self.attributes[index] = value
        for offset, red, green, blue in msg.palette:
            if offset < 256:
                self.pal_r[offset] = red
                self.pal_g[offset] = green
                self.pal_b[offset] = blue
        if self.bpp < 8:
            self.convert_attributes_to_palette(0, 0, self.width, self.height)

    def convert_attributes_to_palette(self, x: int, y: int, w: int, h: int) -> None:
        """Resolve ``attribute_screen`` -> ``palette_screen`` for a region.

        Mirrors ``PixelBuffer.convertAttributesToPalette``: each attribute
        index (0..63) selects one of the 64 attribute-table entries, offset by
        ``color_index<<4`` in "special 4bpp" mode (SPECIAL_BLT_BIT_4BPP).
        """
        base = self.color_index << 4
        special = False
        extra = 0
        if self.is_special_4bpp_mode and len(self.attributes) > 20:
            special = (self.attributes[16] & 0x80) != 0
            if special:
                extra = (self.attributes[20] & 3) << 4
        h = min(h, self.height - y)
        for row in range(h):
            row_off = (y + row) * self.width + x
            for col in range(w):
                idx = row_off + col
                if idx >= len(self.attribute_screen):
                    continue
                attr_idx = (self.attribute_screen[idx] | base) & 0x3F
                val = (
                    self.attributes[attr_idx] if attr_idx < len(self.attributes) else 0
                )
                if special:
                    val &= 0xCF
                self.palette_screen[idx] = (val | extra) & 0xFF

    # -- BitBlt (raw copy / text mode) ---------------------------------------

    def process_bit_blt(self, msg: p.BitBlt) -> tuple[int, int, int, int]:
        """Apply a ``BitBlt`` update; returns the invalidated (x, y, w, h)."""
        x, y, w, h = msg.dest.x, msg.dest.y, msg.dest.width, msg.dest.height
        if y + h > self.height:
            h -= y + h - self.height
        data = msg.data

        if self.bpp > 8:
            self._blit_raw_color(x, y, w, h, data)
        elif msg.blit_type in p.TEXT_BLIT_TYPES:
            self.blit_type = msg.blit_type
            self.font_height = msg.font_height
            self.font_width = msg.font_width
            self._image_rect(x, y, w, h, data)
            x, y, w, h = (
                x * msg.font_width,
                y * msg.font_height,
                w * msg.font_width,
                h * msg.font_height,
            )
            self.convert_attributes_to_palette(x, y, w, h)
        elif self.bpp == 4:
            self._blit_4bpp_attr(x, y, w, h, data)
            self.convert_attributes_to_palette(x, y, w, h)
        else:
            self._blit_indexed(x, y, w, h, data)
        return (x, y, w, h)

    def _blit_raw_color(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        # ``w`` is also the source row stride, so it can't be clipped outright
        # (that would misalign every row after the first) -- clip only the
        # destination write width per row, dropping pixels past the right edge.
        bypp = self.bpp >> 3
        dw = max(0, min(w, self.width - x))
        src = 0
        for row in range(h):
            dst = (y + row) * self.width + x
            s = src
            for _ in range(dw):
                px = data[s] & 0xFF
                px |= (data[s + 1] & 0xFF) << 8
                if self.bpp > 16:
                    px |= (data[s + 2] & 0xFF) << 16
                self.pixel_screen[dst] = px
                dst += 1
                s += bypp
            src += w * bypp

    def _blit_indexed(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        dw = max(0, min(w, self.width - x))
        src = 0
        for row in range(h):
            dst = (y + row) * self.width + x
            self.palette_screen[dst : dst + dw] = data[src : src + dw]
            src += w

    def _blit_4bpp_attr(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        src = 0
        row_limit = self.width - x
        for row in range(h):
            dst = (y + row) * self.width + x
            for col in range(w - 1, -1, -1):
                b = data[src >> 1]
                if (col & 1) == 0:
                    b >>= 4
                if col < row_limit:
                    self.attribute_screen[dst + col] = b & 0xF
                src += 1

    # -- Text mode glyph rendering (TextMode.imageRect) ----------------------

    def _image_rect(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        off = 0
        if self.blit_type == p.BLT_TEXT_ASCII_ATTRIBUTE_COMBO:
            self.attribute_table_valid = True
            n = self.text_rows * self.text_cols * 2
            i = 0
            k = 0
            while i < n:
                self.ascii_table[k] = data[i]
                self.attribute_table[k] = data[i + 1]
                i += 2
                k += 1
        else:
            bt = self.blit_type
            if bt & _BLT_HAS_ASCII:
                n = min(len(self.ascii_table), w * h)
                self.ascii_table[0:n] = data[off : off + n]
                off += n
            if bt & _BLT_HAS_ATTRIBUTE:
                self.attribute_table_valid = True
                n = min(len(self.attribute_table), w * h)
                self.attribute_table[0:n] = data[off : off + n]
                off += n
            if bt & _BLT_HAS_FONT:
                n = min(len(data) - off, len(self.font_table))
                self.font_table[0:n] = data[off : off + n]
                off += n

        stride = self.width
        base = y * self.font_height * stride + x * self.font_width
        row_cursor = 0
        for i in range(h):
            cell_y = y + i
            row_base_idx = i * w
            for j in range(self.font_height):
                # A single accumulating pixel cursor for the whole scanline.
                # This must NOT be reset per character cell: an earlier version did
                # `dst = dst_row` inside the `k` loop, which made every
                # character overwrite the same first font_width pixels instead
                # of advancing — the on-screen symptom was a blank console
                # with only the (independently-drawn) cursor visible, caught
                # by comparing decoded ascii_table content (correct) against
                # the live-rendered PNG (blank) against a real Proxmox login
                # screen.
                dst = base + row_cursor * stride
                for k in range(w):
                    cell_x = x + k
                    idx = row_base_idx + k
                    glyph = self.ascii_table[idx] & 0xFF
                    is_cursor = (
                        self.cursor_y == cell_y
                        and self.cursor_x == cell_x
                        and self.start_line <= j <= self.stop_line
                    )
                    fg, bg = 1, 0
                    if self.attribute_table_valid:
                        attr = self.attribute_table[idx]
                        fg = attr & 0xFF
                        bg = (attr >> 4) & 0xFF
                        if self.is_dbe_swap:
                            fg &= 7
                            glyph += (
                                self.primary_offset
                                if (attr & 8)
                                else self.secondary_offset
                            )
                        else:
                            fg &= 0xF
                        bg = (bg & 7) if self.is_blinking else (bg & 0xF)
                        if self.is_blinking and (attr & 0x80):
                            fg = bg
                    glyph_row = (
                        self.font_table[glyph * 32 + j]
                        if glyph * 32 + j < len(self.font_table)
                        else 0
                    )
                    for bit in range(self.font_width - 1, -1, -1):
                        set_bit = is_cursor or ((glyph_row >> bit) & 1) != 0
                        self.attribute_screen[dst] = fg if set_bit else bg
                        dst += 1
                row_cursor += 1

    def set_text_cursor(self, msg: p.SetTextCursor) -> None:
        """Update text-mode cursor position/shape and redraw it into the screen."""
        self.start_line, self.stop_line = msg.start_line, msg.stop_line
        if self.start_line == self.stop_line:
            self.cursor_x = -1
            self.cursor_y = -1
            return
        self.cursor_x, self.cursor_y = msg.x, msg.y
        self._draw_cursor()
        self.convert_attributes_to_palette(0, 0, self.width, self.height)

    def _draw_cursor(self) -> None:
        stride = self.width
        x0 = self.cursor_x * self.font_width
        base = self.cursor_y * self.font_height * stride + x0
        fg = 1
        if self.attribute_table_valid:
            idx = self.cursor_y * self.text_cols + self.cursor_x
            attr = self.attribute_table[idx]
            fg = (attr & 7) if self.is_dbe_swap else (attr & 0xF)
            bg = (attr >> 4) & 0xFF
            bg = (bg & 7) if self.is_blinking else (bg & 0xF)
            if self.is_blinking and (attr & 0x80):
                fg = bg
        for line in range(self.start_line, self.stop_line + 1):
            row = base + line * stride
            for i in range(self.font_width):
                if 0 <= row + i < len(self.attribute_screen):
                    self.attribute_screen[row + i] = fg

    def apply_special_graphics_bit(self, msg: p.SpecialGraphicsBit) -> None:
        """Apply blink/DBE-swap/4bpp-color-index flags from a
        ``SpecialGraphicsBit`` message."""
        self.is_blinking = msg.blinking_text
        self.is_dbe_swap = msg.is_swapping
        self.primary_offset = msg.primary_select_offset
        self.secondary_offset = msg.secondary_select_offset
        self.color_index = msg.bpp4_color_index

    # -- EnhanceBitBlt (tiled dirty-rect raw copy) ---------------------------

    def process_enhance_bit_blt(
        self, msg: p.EnhanceBitBlt
    ) -> tuple[int, int, int, int] | None:
        """Apply an uncompressed ``EnhanceBitBlt``; returns the
        invalidated rect or None if no tile was dirty."""
        cursor = _RawTileCursor(msg.data, self.bpp)
        return self._enhance_tiles(
            msg, lambda tx, ty, tw, th: self._write_raw_tile(cursor, tx, ty, tw, th)
        )

    def process_enhance_bit_blt_hlc(
        self, msg: p.EnhanceBitBlt
    ) -> tuple[int, int, int, int] | None:
        """Apply an HLC (byte-RLE) compressed ``EnhanceBitBlt``; returns
        the invalidated rect or None."""
        decoder = _HlcChannelDecoder(msg.data, msg.triplet_code, msg.repeat_code)
        offsets = [0]
        if self.bpp > 8:
            n0 = _le32(msg.data, 0)
            n1 = _le32(msg.data, 4)
            header = 8
            if self.bpp > 16:
                header = 12
                offsets = [header, header + n0, header + n0 + n1]
            else:
                offsets = [header, header + n0]
        decoder.set_channel_offsets(offsets)
        return self._enhance_tiles(
            msg, lambda tx, ty, tw, th: self._write_hlc_tile(decoder, tx, ty, tw, th)
        )

    def process_enhance_bit_blt_4bpp(
        self, msg: p.EnhanceBitBlt
    ) -> tuple[int, int, int, int] | None:
        """Apply a nibble-packed 4bpp ``EnhanceBitBlt`` and re-resolve
        the palette for the dirty rect."""
        self.is_special_4bpp_mode = True
        cursor = [0]  # single-element mutable cell: nibble cursor, shared across tiles
        result = self._enhance_tiles(
            msg,
            lambda tx, ty, tw, th: self._write_4bpp_tile(
                msg.data, cursor, tx, ty, tw, th
            ),
        )
        if result is not None:
            self.convert_attributes_to_palette(*result)
        return result

    def _enhance_tiles(
        self, msg: p.EnhanceBitBlt, write_tile
    ) -> tuple[int, int, int, int] | None:
        """Walk the dirty-tile snoop bitmaps, calling ``write_tile`` per
        dirty tile, and return the overall invalidated (x, y, w, h)
        bounding rect (or None if none dirty)."""
        tw, th = msg.tile_width, msg.tile_height
        x_shift = _shift_amount(tw)
        y_shift = _shift_amount(th)
        n_cols = min(_num_tiles(self.width, tw), 64)
        n_rows = min(_num_tiles(self.height, th), 64)

        min_col, max_col = None, None
        min_row, max_row = None, None
        for row in range(n_rows):
            low, high = msg.snoop_low[row], msg.snoop_high[row]
            for col in range(n_cols):
                dirty = (low & (1 << col)) if col < 32 else (high & (1 << (col - 32)))
                if not dirty:
                    continue
                if min_row is None:
                    min_row = row
                max_row = row
                min_col = col if min_col is None else min(min_col, col)
                max_col = col if max_col is None else max(max_col, col)
                write_tile(col << x_shift, row << y_shift, tw, th)
        if min_col is None:
            return None
        x = min_col << x_shift
        y = min_row << y_shift
        w = min((max_col + 1) << x_shift, self.width) - x
        h = min((max_row + 1) << y_shift, self.height) - y
        return (x, y, w, h)

    def _write_raw_tile(
        self, cursor: _RawTileCursor, tx: int, ty: int, tw: int, th: int
    ) -> None:
        """Raw (uncompressed) per-tile pixel copy, matching ``enhanceBitBlt``.

        The source cursor advances by a full tile-worth of pixels for every
        dirty tile regardless of clipping — a row that falls off the bottom of
        the framebuffer still consumes its bytes, it just isn't written.
        """
        for row in range(th):
            py = ty + row
            in_bounds_row = py < self.height
            dst = py * self.width + tx if in_bounds_row else 0
            for col in range(tw):
                r, g, b = cursor.next_pixel()
                if in_bounds_row and tx + col < self.width:
                    if self.bpp > 8:
                        self.pixel_screen[dst + col] = (r << 16) | (g << 8) | b
                    else:
                        self.palette_screen[dst + col] = r & 0xFF

    def _write_hlc_tile(
        self, decoder: _HlcChannelDecoder, tx: int, ty: int, tw: int, th: int
    ) -> None:
        """Decode one HLC tile's worth of pixels and write them into the
        screen buffer."""
        for row in range(th):
            py = ty + row
            if py >= self.height:
                continue
            dst = py * self.width + tx
            for col in range(tw):
                r, g, b = decoder.next_pixel(self.bpp)
                if 0 <= tx + col < self.width:
                    if self.bpp > 8:
                        self.pixel_screen[dst + col] = (r << 16) | (g << 8) | b
                    else:
                        self.palette_screen[dst + col] = r & 0xFF

    def _write_4bpp_tile(
        self, data: bytes, cursor: list[int], tx: int, ty: int, tw: int, th: int
    ) -> None:
        """Nibble-packed (2 px/byte, low nibble first) tile write into
        ``attribute_screen``, matching ``enhanceBitBlt4bpp``'s shared nibble
        cursor across all dirty tiles (advances even for clipped rows)."""
        for row in range(th):
            py = ty + row
            in_bounds_row = py < self.height
            dst = py * self.width + tx if in_bounds_row else 0
            for col in range(tw):
                nib = cursor[0]
                b = data[nib >> 1]
                val = b & 0xF if (nib & 1) == 0 else (b >> 4) & 0xF
                cursor[0] = nib + 1
                if in_bounds_row and tx + col < self.width:
                    self.attribute_screen[dst + col] = val


def _le32(data: bytes, off: int) -> int:
    return (
        data[off] | (data[off + 1] << 8) | (data[off + 2] << 16) | (data[off + 3] << 24)
    )


def _shift_amount(n: int) -> int:
    shift = 1
    while (n >> shift) != 0:
        shift += 1
    return shift - 1


def _num_tiles(total: int, tile: int) -> int:
    q, r = divmod(total, tile)
    return q + (1 if r else 0)


class _RawTileCursor:
    """Sequential byte cursor for the uncompressed ``EnhanceBitBlt`` payload.

    Same BGR byte layout as plain ``BitBlt``: 1 byte/pixel palette index for
    bpp<=8, else blue/green[/red] bytes for bpp 16/24/32.
    """

    def __init__(self, data: bytes, bpp: int) -> None:
        self._data = data
        self._bpp = bpp
        self._bypp = max(1, bpp >> 3)
        self._pos = 0

    def next_pixel(self) -> tuple[int, int, int]:
        """Return the next (r, g, b) pixel from the raw tile stream and
        advance past it."""
        data, pos = self._data, self._pos
        if self._bpp <= 8:
            r = data[pos] & 0xFF
            self._pos += self._bypp
            return (r, 0, 0)
        blue = data[pos] & 0xFF
        green = data[pos + 1] & 0xFF
        red = (data[pos + 2] & 0xFF) if self._bpp > 16 else 0
        self._pos += self._bypp
        return (red, green, blue)


class _HlcChannelDecoder:
    """Per-channel byte-RLE decoder for EnhanceBitBltHLC.

    Each channel (blue/green/red, or a single palette-index channel for
    bpp<=8) is its own byte stream with two escape bytes: ``triplet_code``
    (next byte repeats 3x) and ``repeat_code`` (next byte is a repeat count,
    with count 0/1 as literal escapes for the two special byte values
    themselves). Matches ``PixelBufferImage.enhanceBitBltHLC``'s inline state
    machine, generalized to N channels driven by parallel cursors.
    """

    def __init__(self, data: bytes, triplet_code: int, repeat_code: int) -> None:
        self._data = data
        self._triplet = triplet_code
        self._repeat = repeat_code
        self._offsets: list[int] = [0]
        self._remaining: list[int] = [0]
        self._value: list[int] = [0]

    def set_channel_offsets(self, offsets: list[int]) -> None:
        """Reset each channel's cursor to start at the given byte offset."""
        self._offsets = list(offsets)
        self._remaining = [0] * len(offsets)
        self._value = [0] * len(offsets)

    def _next(self, ch: int) -> int:
        if self._remaining[ch] < 1:
            data = self._data
            off = self._offsets[ch]
            v = data[off] & 0xFF
            off += 1
            if v == self._triplet:
                run = 3
                v = data[off] & 0xFF
                off += 1
            elif v == self._repeat:
                count = data[off] & 0xFF
                off += 1
                if count == 1:
                    run, v = 1, self._triplet
                elif count == 0:
                    run, v = 1, self._repeat
                else:
                    run = count + 1
                    v = data[off] & 0xFF
                    off += 1
            else:
                run = 1
            self._offsets[ch] = off
            self._remaining[ch] = run
            self._value[ch] = v
        value = self._value[ch]
        self._remaining[ch] -= 1
        return value

    def next_pixel(self, bpp: int) -> tuple[int, int, int]:
        """Decode the next (r, g, b) pixel by pulling one RLE-expanded
        byte per channel."""
        if bpp <= 8:
            return (self._next(0), 0, 0)
        blue = self._next(0)
        green = self._next(1)
        red = self._next(2) if bpp > 16 else 0
        return (red, green, blue)
