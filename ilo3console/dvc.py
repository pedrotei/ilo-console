"""The DVC video decoder — iLO 3's proprietary console video codec.

The console channel delivers video as a bit-level stream that a 48-state machine
decodes into 16x16 (or 16x8 "half height") pixel blocks, with a 17-entry colour
LRU cache, run-length and block-repeat coding, and embedded side-band commands
(power/health status, on-screen text, framerate, and — crucially — the command
that turns stream encryption on and selects the cipher).

See docs/PROTOCOL_ILO3.md's "DVC video codec" section for the full state
machine and opcode reference. Decoder state (including lookup tables like
``bits_to_read``/``next_1``) is kept per-instance so multiple sessions can
decode independently.

Feed one *plaintext* byte at a time via :meth:`DvcDecoder.feed`. Decoded blocks
land in the :class:`Framebuffer`; side-band events go to a :class:`DvcController`.
"""

from __future__ import annotations

# ---- decoder states (indices into the transition tables) -------------------
RESET = 0
START = 1
PIXELS = 2
PIXLRU1 = 3
PIXLRU0 = 4
PIXCODE1 = 5
PIXCODE2 = 6
PIXCODE3 = 7
PIXGREY = 8
PIXRGBR = 9
PIXRPT = 10
PIXRPT1 = 11
PIXRPTSTD1 = 12
PIXRPTSTD2 = 13
PIXRPTNSTD = 14
CMD = 15
CMD0 = 16
MOVEXY0 = 17
EXTCMD = 18
CMDX = 19
MOVESHORTX = 20
MOVELONGX = 21
BLKRPT = 22
EXTCMD1 = 23
FIRMWARE = 24
EXTCMD2 = 25
MODE0 = 26
TIMEOUT = 27
BLKRPT1 = 28
BLKRPTSTD = 29
BLKRPTNSTD = 30
PIXFAN = 31
PIXCODE4 = 32
PIXDUP = 33
BLKDUP = 34
PIXCODE = 35
PIXSPEC = 36
EXIT = 37
LATCHED = 38
MOVEXY1 = 39
MODE1 = 40
PIXRGBG = 41
PIXRGBB = 42
HUNT = 43
PRINT0 = 44
PRINT1 = 45
CORP = 46
MODE2 = 47

# Per-state bit counts and transition tables (cim.bits_to_read/next_0/next_1).
# bits_to_read is per-instance because SetHalfHeight / setBitsPerColor mutate it.
_BITS_TO_READ = [0, 1, 1, 1, 1, 1, 2, 3, 5, 5, 1, 1, 3, 3, 8, 1, 1, 7, 1, 1, 3,
                 7, 1, 1, 8, 1, 7, 0, 1, 3, 7, 1, 4, 0, 0, 0, 1, 0, 1, 7, 7, 5,
                 5, 1, 8, 8, 1, 4]
_NEXT_0 = [1, 2, 31, 2, 2, 10, 10, 10, 10, 41, 2, 33, 2, 2, 2, 16, 19, 39, 22,
           20, 1, 1, 34, 25, 46, 26, 40, 1, 29, 1, 1, 36, 10, 2, 1, 35, 8, 37,
           38, 1, 47, 42, 10, 43, 45, 45, 1, 1]
_NEXT_1 = [1, 15, 3, 11, 11, 10, 10, 10, 10, 41, 11, 12, 2, 2, 2, 17, 18, 39,
           23, 21, 1, 1, 28, 24, 46, 27, 40, 1, 30, 1, 1, 35, 10, 2, 1, 35, 9,
           37, 38, 1, 47, 42, 10, 0, 45, 45, 24, 1]
_GETMASK = [0, 1, 3, 7, 15, 31, 63, 127, 255]


class Framebuffer:
    """A flat RGB framebuffer that receives decoded pixel blocks.

    Pixels are stored as ``0x00RRGGBB`` ints (as produced by the colour remap
    table). ``generation`` bumps on every resize/clear so consumers can detect a
    mode change.
    """

    def __init__(self, width: int = 0, height: int = 0) -> None:
        self.width = width
        self.height = height
        self.pixels = [0] * (width * height)
        self.generation = 0

    def resize(self, width: int, height: int) -> None:
        """Resize to a new width/height, zeroing all pixels and bumping
        ``generation``.
        """
        self.width = width
        self.height = height
        self.pixels = [0] * (width * height)
        self.generation += 1

    def clear(self) -> None:
        """Zero all pixels at the current size and bump ``generation``."""
        self.pixels = [0] * (self.width * self.height)
        self.generation += 1

    def paste_block(self, block, x: int, y: int, w: int, h: int) -> None:
        """Copy a ``w``-wide, ``h``-tall block (row-major in ``block``) at (x,y)."""
        fb_w = self.width
        for row in range(h):
            fy = y + row
            if fy < 0 or fy >= self.height:
                continue
            dst = fy * fb_w + x
            src = row * 16  # blocks are always 16 wide in the source buffer
            for col in range(w):
                fx = x + col
                if 0 <= fx < fb_w:
                    self.pixels[dst + col] = block[src + col]

    def to_rgb_bytes(self) -> bytes:
        """Serialise as packed RGB (3 bytes/pixel), row-major."""
        out = bytearray(self.width * self.height * 3)
        for i, px in enumerate(self.pixels):
            out[i * 3] = (px >> 16) & 0xFF
            out[i * 3 + 1] = (px >> 8) & 0xFF
            out[i * 3 + 2] = px & 0xFF
        return bytes(out)


class DvcController:
    """Side-band event sink for the decoder. Override what you care about.

    ``set_cipher`` is the important one: the decoder calls it when the firmware's
    header/encryption command arrives, and the console channel uses it to switch
    its keystreams. ``send_ack`` / ``refresh_screen`` let the decoder talk back.
    """

    def set_cipher(self, cipher: int) -> None:
        """Called when the header/encryption command selects a stream cipher."""

    def send_ack(self) -> None:
        """Called when the decoder needs the console channel to send an ack."""

    def refresh_screen(self) -> None:
        """Called when the decoder detects a stuck/latched state and wants
        a full redraw.
        """

    def on_power(self, on: bool) -> None:
        """Called when the server's power state changes."""

    def on_health(self, level: int) -> None:
        """Called when the server's health/status LED level changes."""

    def on_licensed(self, flags: int) -> None:
        """Called with the licensing flags reported by the firmware."""

    def on_flags(self, flags: int) -> None:
        """Called with miscellaneous firmware feature flags."""

    def on_framerate(self, fps: int) -> None:
        """Called when the firmware reports its target frame rate."""

    def on_status(self, field: int, text: str) -> None:
        """Called with a status-line update for the given field."""

    def on_text(self, text: str) -> None:
        """Called with on-screen text decoded from the print channel."""

    def on_resize(self, width: int, height: int) -> None:
        """Called when the video mode changes to a new resolution."""

    def on_frame(self) -> None:
        """Called when a full frame has been decoded and is ready to display."""

    def on_seize(self) -> None:
        """Called when another client seizes control of the console."""

    def on_exit(self) -> None:
        """Called when the decoder sees the stream-exit command."""


class DvcDecoder:
    """Stateful DVC bit-stream decoder. Not thread-safe; feed from one thread."""

    def __init__(self, framebuffer: Framebuffer, controller: DvcController) -> None:
        self.fb = framebuffer
        self.ctl = controller

        self.bits_to_read = list(_BITS_TO_READ)
        self.next_1 = list(_NEXT_1)  # entry [31] is mutated as the cache fills

        self._init_reversal()

        # Bit accumulator.
        self.ib_acc = 0
        self.ib_bcnt = 0
        self.zero_count = 0

        # Decoder registers.
        self.state = RESET
        self.next_state = RESET
        self.code = 0
        self.count_bytes = 0

        # Block geometry.
        self.block_width = 16
        self.block_height = 16
        self.bits_per_color = 5
        self.color_remap = [0] * 32768
        self._build_pixel_table(5)

        # Colour cache (LRU).
        self.cc_active = 0
        self.cc_color = [0] * 17
        self.cc_usage = [0] * 17
        self.cc_block = [0] * 17
        self.pixcode = LATCHED

        # Current block + placement.
        self.block = [0] * 256
        self.pixel_count = 0
        self.lastx = 0
        self.lasty = 0
        self.newx = 0
        self.newy = 0
        self.size_x = 0
        self.size_y = 0
        self.y_clipped = 0
        self.screen_x = 0
        self.screen_y = 0
        self.video_detected = True

        # Pixel colour components.
        self.red = 0
        self.green = 0
        self.blue = 0
        self.last_color = 0

        # Command + print channels.
        self.cmd_buf = [0] * 256
        self.cmd_count = 0
        self.cmd_last = 0
        self.print_chan = 0
        self.print_str = ""

        # Diagnostics / resync.
        self.fatal_count = 0
        self.timeout_count = -1

    # ---- bit-reversal / zero-run tables ------------------------------------

    def _init_reversal(self):
        self.reversal = [0] * 256
        self.left = [0] * 256
        self.right = [0] * 256
        for n in range(256):
            lo_min, hi = 8, 8
            x = n
            out = 0
            for bit in range(8):
                out <<= 1
                if x & 1:
                    lo_min = min(lo_min, bit)
                    out |= 1
                    hi = 7 - bit
                x >>= 1
            self.reversal[n] = out
            self.right[n] = lo_min
            self.left[n] = hi

    # ---- bit input ----------------------------------------------------------

    def _add_bits(self, c: int) -> int:
        self.ib_acc |= c << self.ib_bcnt
        self.ib_bcnt += 8
        self.zero_count += self.right[c]
        if self.zero_count > 30:
            self.next_state = HUNT
            self.state = HUNT
            return 4
        if c != 0:
            self.zero_count = self.left[c]
        return 0

    def _get_bits(self, n: int) -> None:
        if n == 1:
            self.code = self.ib_acc & 1
            self.ib_acc >>= 1
            self.ib_bcnt -= 1
            return
        if n == 0:
            return
        val = self.ib_acc & _GETMASK[n]
        self.ib_bcnt -= n
        self.ib_acc >>= n
        val = self.reversal[val]
        self.code = val >> (8 - n)

    # ---- colour cache -------------------------------------------------------

    def _cache_reset(self):
        self.cc_active = 0

    def _pixcode_for(self, active: int) -> int:
        if active < 2:
            return LATCHED
        if active == 2:
            return PIXLRU0
        if active == 3:
            return PIXCODE1
        if active < 6:
            return PIXCODE2
        if active < 10:
            return PIXCODE3
        return PIXCODE4

    def _cache_lru(self, color: int) -> int:
        active = self.cc_active
        idx = 0
        found = 0
        for i in range(active):
            if color == self.cc_color[i]:
                idx = i
                found = 1
                break
            if self.cc_usage[i] == active - 1:
                idx = i
        threshold = self.cc_usage[idx]
        if found == 0:
            if active < 17:
                idx = active
                threshold = active
                active += 1
                self.cc_active = active
                self.pixcode = self._pixcode_for(active)
                self.next_1[PIXFAN] = self.pixcode
            self.cc_color[idx] = color
        self.cc_block[idx] = 1
        for i in range(active):
            if self.cc_usage[i] < threshold:
                self.cc_usage[i] += 1
        self.cc_usage[idx] = 0
        return found

    def _cache_find(self, index: int) -> int:
        active = self.cc_active
        for i in range(active):
            if index == self.cc_usage[i]:
                color = self.cc_color[i]
                for j in range(active):
                    if self.cc_usage[j] < index:
                        self.cc_usage[j] += 1
                self.cc_usage[i] = 0
                self.cc_block[i] = 1
                return color
        return -1

    def _cache_prune(self):
        n = self.cc_active
        i = 0
        while i < n:
            if self.cc_block[i] == 0:
                n -= 1
                self.cc_block[i] = self.cc_block[n]
                self.cc_color[i] = self.cc_color[n]
                self.cc_usage[i] = self.cc_usage[n]
            else:
                self.cc_block[i] -= 1
                i += 1
        self.cc_active = n
        self.pixcode = self._pixcode_for(n)
        self.next_1[PIXFAN] = self.pixcode

    # ---- colour depth -------------------------------------------------------

    def _build_pixel_table(self, bpc: int):
        n2 = 1 << (bpc * 3)
        t = self.color_remap
        if bpc == 5:
            for v in range(n2):
                t[v] = ((v & 0x1F) << 3) | ((v & 0x3E0) << 6) | ((v & 0x7C00) << 9)
        elif bpc == 4:
            for v in range(n2):
                t[v] = ((v & 0xF) << 4) | ((v & 0xF0) << 8) | ((v & 0xF00) << 12)
        elif bpc == 3:
            for v in range(n2):
                t[v] = ((v & 0xF) << 5) | ((v & 0xF0) << 11) | ((v & 0xF00) << 15)
        elif bpc == 2:
            for v in range(n2):
                t[v] = ((v & 0xF) << 6) | ((v & 0xF0) << 15) | ((v & 0xF00) << 18)

    def _set_bits_per_color(self, n: int):
        self.bits_per_color = 5 - (n & 3)
        for s in (PIXGREY, PIXRGBR, PIXRGBG, PIXRGBB):
            self.bits_to_read[s] = self.bits_per_color
        self._build_pixel_table(self.bits_per_color)

    def _set_half_height(self):
        if self.screen_x > 1616:
            # Half-height (8px blocks) is only used on very wide modes; we
            # always accept it rather than warning for incapable hardware.
            if self.block_height != 8:
                self.block_height = 8
                for s in (MOVELONGX, MOVEXY0, MOVEXY1, BLKRPTNSTD):
                    self.bits_to_read[s] = 8
        elif self.block_height != 16:
            self.block_height = 16
            for s in (MOVELONGX, MOVEXY0, MOVEXY1, BLKRPTNSTD):
                self.bits_to_read[s] = 7

    # ---- block placement ----------------------------------------------------

    def _next_block(self, count: int):
        paint = self.video_detected
        if self.pixel_count != 0 and self.y_clipped > 0 and self.lasty == self.size_y:
            fill = self.color_remap[0]
            for i in range(self.y_clipped, 256):
                self.block[i] = fill
        self.pixel_count = 0
        self.next_state = START
        px = self.lastx * self.block_width
        py = self.lasty * self.block_height
        while count != 0:
            if paint:
                self.fb.paste_block(self.block, px, py, 16, self.block_height)
            px += 16
            self.lastx += 1
            if self.lastx >= self.size_x:
                break
            count -= 1

    # ---- main entry ---------------------------------------------------------

    def feed(self, byte: int) -> int:
        """Process one decrypted stream byte. Returns the process code (0=ok)."""
        return self._process_bits(byte & 0xFF)

    def _push_pixel(self, value: int) -> bool:
        """Append one pixel; returns False if the block overflowed (→ LATCHED)."""
        if self.pixel_count >= self.block_height * self.block_width:
            self.next_state = LATCHED
            return False
        self.block[self.pixel_count] = value
        self.pixel_count += 1
        return True

    def _process_bits(self, c: int) -> int:
        self._add_bits(c)
        self.count_bytes += 1
        n = 0
        while n == 0:
            need = self.bits_to_read[self.state]
            if need > self.ib_bcnt:
                return 0
            self._get_bits(need)
            self.next_state = (
                _NEXT_0[self.state] if self.code == 0 else self.next_1[self.state]
            )
            st = self.state
            hung_ok = self._dispatch(st)
            if not hung_ok:
                n = 6
                break

            if self.next_state == PIXELS and self.pixel_count == (
                self.block_height * self.block_width
            ):
                self._next_block(1)
                self._cache_prune()

            if self.state == self.next_state and self.state not in (
                PRINT1, LATCHED, HUNT,
            ):
                n = 6
                break
            self.state = self.next_state
            if st == EXIT:
                return 1
        return n

    def _dispatch(self, st: int) -> bool:
        """Run the per-state action. Returns False only on a detected hang."""
        if st in (PIXLRU1, PIXLRU0, PIXCODE1, PIXCODE2, PIXCODE3, PIXCODE4):
            if self.cc_active == 1:
                self.code = self.cc_usage[0]
            elif st == PIXLRU0:
                self.code = 0
            elif st == PIXLRU1:
                self.code = 1
            elif self.code != 0:
                self.code += 1
            color = self._cache_find(self.code)
            if color == -1:
                self.next_state = LATCHED
                return True
            self.last_color = self.color_remap[color]
            self._push_pixel(self.last_color)
            return True

        if st == PIXRPTSTD1:
            if self.code == 7:
                self.next_state = PIXRPTNSTD
                return True
            if self.code == 6:
                self.next_state = PIXRPTSTD2
                return True
            self.code += 2
            for _ in range(self.code):
                if not self._push_pixel(self.last_color):
                    return True
            return True

        if st == PIXRPTSTD2:
            self.code += 8
            for _ in range(self.code):  # fallthrough to PIXRPTNSTD
                if not self._push_pixel(self.last_color):
                    return True
            return True

        if st == PIXRPTNSTD:
            for _ in range(self.code):
                if not self._push_pixel(self.last_color):
                    return True
            return True

        if st == PIXDUP:
            self._push_pixel(self.last_color)
            return True

        if st == PIXCODE:
            self.next_state = self.pixcode
            return True

        if st == PIXRGBR:
            self.red = self.code << (self.bits_per_color * 2)
            return True

        if st == PIXRGBG:
            self.green = self.code << self.bits_per_color
            return True

        if st == PIXGREY:
            self.red = self.code << (self.bits_per_color * 2)
            self.green = self.code << self.bits_per_color
            # fallthrough to PIXRGBB
            return self._pix_rgbb()

        if st == PIXRGBB:
            return self._pix_rgbb()

        if st in (MOVEXY0, MODE0):
            self.newx = self.code
            if st == MOVEXY0 and self.newx > self.size_x:
                self.newx = 0
            return True

        if st == MOVEXY1:
            self.newy = self.code
            if self.block_height == 16:
                self.newy &= 0x7F
            self.lastx = self.newx
            self.lasty = self.newy
            return True

        if st == MOVESHORTX:
            self.code = self.lastx + self.code + 1
            # fallthrough to MOVELONGX
            self.lastx = self.code
            if self.block_height == 16:
                self.lastx &= 0x7F
            return True

        if st == MOVELONGX:
            self.lastx = self.code
            if self.block_height == 16:
                self.lastx &= 0x7F
            return True

        if st == TIMEOUT:
            if self.timeout_count == self.count_bytes - 1:
                self.next_state = LATCHED
            if (self.ib_bcnt & 7) != 0:
                self._get_bits(self.ib_bcnt & 7)
            self.timeout_count = self.count_bytes
            self.ctl.on_frame()
            return True

        if st == FIRMWARE:
            if self.cmd_count != 0:
                self.cmd_buf[self.cmd_count - 1] = self.cmd_last
            self.cmd_count += 1
            self.cmd_last = self.code
            return True

        if st == CORP:
            if self.code != 0:
                return True
            self._dispatch_command()
            self.cmd_count = 0
            return True

        if st == PRINT0:
            self.print_chan = self.code
            self.print_str = ""
            return True

        if st == PRINT1:
            if self.code != 0:
                self.print_str += chr(self.code)
            else:
                if self.print_chan in (1, 2):
                    self.ctl.on_status(2 + self.print_chan, self.print_str)
                elif self.print_chan == 4:
                    self.ctl.on_text(self.print_str)
                self.next_state = START
            return True

        if st == RESET:
            self._cache_reset()
            self.pixel_count = 0
            self.lastx = 0
            self.lasty = 0
            self.red = self.green = self.blue = 0
            self.fatal_count = 0
            self.timeout_count = -1
            self.cmd_count = 0
            return True

        if st == LATCHED:
            if self.fatal_count in (40, 11680):
                self.ctl.refresh_screen()
            self.fatal_count += 1
            if self.fatal_count == 120000:
                self.ctl.refresh_screen()
            if self.fatal_count == 12000000:
                self.ctl.refresh_screen()
                self.fatal_count = 41
            return True

        if st == BLKDUP:
            self._next_block(1)
            return True

        if st == BLKRPTSTD:
            self.code += 2
            self._next_block(self.code)  # fallthrough to BLKRPTNSTD
            return True

        if st == BLKRPTNSTD:
            self._next_block(self.code)
            return True

        if st == MODE1:
            self.size_x = self.newx
            self.size_y = self.code
            return True

        if st == MODE2:
            self._mode2()
            return True

        if st == HUNT:
            if self.next_state != HUNT:
                self.ib_bcnt = 0
                self.ib_acc = 0
                self.zero_count = 0
                self.count_bytes = 0
            return True

        if st == EXIT:
            self.ctl.on_exit()
            return True

        # States with no side effect (START, PIXELS, PIXRPT, PIXRPT1, BLKRPT,
        # BLKRPT1, PIXFAN, PIXSPEC, CMD, CMD0, EXTCMD, CMDX, EXTCMD1, EXTCMD2).
        return True

    def _pix_rgbb(self) -> bool:
        self.blue = self.code
        color = self.red | self.green | self.blue
        if self._cache_lru(color) != 0:
            self.next_state = LATCHED
            return True
        self.last_color = self.color_remap[color]
        self._push_pixel(self.last_color)
        return True

    def _dispatch_command(self):
        op = self.cmd_last
        if op == 1:
            self.next_state = EXIT
        elif op == 2:
            self.next_state = PRINT0
        elif op == 3:
            self.ctl.on_framerate(self.cmd_buf[0] if self.cmd_count != 0 else 0)
        elif op == 4:
            self.ctl.on_power(True)
        elif op == 5:
            self.ctl.on_power(False)
            self.fb.clear()
            self.newx = 50
            self.code = 38
        elif op == 6:
            self.fb.clear()
            self.ctl.on_status(2, "")
            self.ctl.on_frame()
        elif op == 7:
            pass  # terminal-services type; not used headless
        elif op == 9:
            if (self.ib_bcnt & 7) != 0:
                self._get_bits(self.ib_bcnt & 7)
        elif op == 10:
            self.ctl.on_seize()
        elif op == 11:
            self._set_bits_per_color(self.cmd_buf[0])
        elif op == 12:
            self._set_video_decryption(self.cmd_buf[0])
        elif op == 13:
            self._set_bits_per_color(self.cmd_buf[0])
            self._set_video_decryption(self.cmd_buf[1])
            self.ctl.on_licensed(self.cmd_buf[2])
            self.ctl.on_flags(self.cmd_buf[3])
        elif op == 16:
            self.ctl.send_ack()
        elif op == 128:
            self.ctl.on_frame()

    def _set_video_decryption(self, cipher: int):
        # 0=none 1=RC4 2=AES128 3=AES256; the controller wires up the keystream.
        self.ctl.set_cipher(cipher if cipher in (0, 1, 2, 3) else 0)

    def _mode2(self):
        self.lastx = 0
        self.lasty = 0
        self.pixel_count = 0
        self._cache_reset()
        self.screen_x = self.size_x * self.block_width
        self.screen_y = self.size_y * 16 + self.code
        self.video_detected = self.screen_x != 0 and self.screen_y != 0
        self.y_clipped = (256 - 16 * self.code) if self.code > 0 else 0
        if not self.video_detected:
            self.fb.clear()
            self.ctl.on_status(2, "no video")
            return
        self.fb.resize(self.screen_x, self.screen_y)
        self._set_half_height()
        self.ctl.on_resize(self.screen_x, self.screen_y)
        self.ctl.on_status(2, f"{self.screen_x}x{self.screen_y}")
