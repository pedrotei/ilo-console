"""The DVC video decoder — iLO 3's proprietary console video codec.

This module implements a stateful bit-stream decoder for Hewlett Packard
Enterprise iLO 3 management console video protocol. The console channel
delivers video as a compressed bit-level stream that a 48-state finite state
machine decodes into 16x16 (or 16x8 "half height") pixel blocks.

Features:
    - Run-length encoding with block-repeat coding for efficient compression
    - 17-entry colour LRU cache for palette management
    - Embedded side-band commands for power/health status, on-screen text,
      framerate reporting, and stream encryption control
    - Support for multiple color depths (3-5 bits per color)
    - Half-height mode detection for wide resolutions

See docs/PROTOCOL_ILO3.md's "DVC video codec" section for the full state
machine and opcode reference. Decoder state (including lookup tables like
``bits_to_read``/``next_1``) is kept per-instance so multiple sessions can
decode independently.

Usage:
    >>> from ilo3console.dvc import DvcDecoder, Framebuffer, DvcController
    >>> fb = Framebuffer(width=1024, height=768)
    >>> controller = MyController()  # subclass to handle events
    >>> decoder = DvcDecoder(fb, controller)
    >>> for byte in encrypted_stream:
    ...     result = decoder.feed(byte)
    ...     if result == 1:
    ...         print("Stream exited")

Feed one *plaintext* (decrypted) byte at a time via :meth:`DvcDecoder.feed`.
Decoded pixel blocks land in the :class:`Framebuffer`; side-band events go to
a :class:`DvcController` instance.
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

    This class provides a simple row-major buffer for storing decoded video
    frames. Pixels are stored as 24-bit integers in ``0x00RRGGBB`` format
    (as produced by the colour remap table). The ``generation`` counter bumps
    on every resize/clear operation, allowing consumers to detect mode changes.

    Attributes:
        width: Current framebuffer width in pixels
        height: Current framebuffer height in pixels
        pixels: Flat list of pixel values (row-major order)
        generation: Counter that increments on resize/clear for change detection

    Example:
        >>> fb = Framebuffer(width=1024, height=768)
        >>> fb.resize(800, 600)  # Change resolution
        >>> fb.clear()  # Clear to black
    """

    def __init__(self, width: int = 0, height: int = 0) -> None:
        """Initialize an empty framebuffer.

        Args:
            width: Initial width in pixels (default 0 for dynamic sizing)
            height: Initial height in pixels (default 0 for dynamic sizing)
        """
        self.width = width
        self.height = height
        self.pixels = [0] * (width * height)
        self.generation = 0

    def resize(self, width: int, height: int) -> None:
        """Resize to a new width/height, zeroing all pixels and bumping ``generation``.

        Args:
            width: New width in pixels
            height: New height in pixels
        """
        self.width = width
        self.height = height
        self.pixels = [0] * (width * height)
        self.generation += 1

    def clear(self) -> None:
        """Zero all pixels at the current size and bump ``generation``.

        This is typically called when receiving a clear-screen command from
        the decoder.
        """
        self.pixels = [0] * (self.width * self.height)
        self.generation += 1

    def paste_block(self, block: list[int], x: int, y: int, w: int, h: int) -> None:
        """Copy a ``w``-wide, ``h``-tall block (row-major in ``block``) at (x,y).

        Args:
            block: Source pixel data (16-wide rows, even for half-height blocks)
            x: Destination x position
            y: Destination y position
            w: Width of the block to copy
            h: Height of the block to copy

        Note:
            The source ``block`` is always treated as 16 pixels wide per row.
            Boundary checking prevents writes outside the framebuffer.
        """
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
        """Serialise as packed RGB (3 bytes/pixel), row-major.

        Returns:
            Bytes in RGB order per pixel (red byte, then green, then blue).

        Example:
            >>> fb = Framebuffer(width=2, height=2)
            >>> fb.pixels = [0xFF0000FF, 0x00FF00FF, 0x0000FFFF, 0xFFFFFFFF]
            >>> rgb_bytes = fb.to_rgb_bytes()
        """
        out = bytearray(self.width * self.height * 3)
        for i, px in enumerate(self.pixels):
            out[i * 3] = (px >> 16) & 0xFF
            out[i * 3 + 1] = (px >> 8) & 0xFF
            out[i * 3 + 2] = px & 0xFF
        return bytes(out)


class DvcController:
    """Side-band event sink for the decoder. Override what you care about.

    This class receives all side-band events from the DVC decoder, including:
    - Stream encryption cipher selection (critical for console channel)
    - Server power state changes
    - Health/status LED updates
    - Licensing and feature flags from firmware
    - On-screen text and status messages
    - Video mode changes (resolution, half-height detection)
    - Frame completion notifications
    - Seize/exit events

    ``set_cipher`` is the most important callback: the decoder invokes it when
    the firmware's header/encryption command arrives, and the console channel
    uses it to switch its keystreams. ``send_ack`` / ``refresh_screen`` let the
    decoder talk back to the controller.

    Example:
        >>> class MyController(DvcController):
        ...     def set_cipher(self, cipher):
        ...         print(f"Encryption cipher: {cipher}")
        ...         self.keystream = init_keystream(cipher)
        ...     def on_frame(self):
        ...         display(self.fb.to_rgb_bytes())

    Args:
        framebuffer: Optional framebuffer to access decoded pixels
        controller: Optional reference back to the decoder
    """

    def __init__(self, framebuffer: Framebuffer | None = None,
                 decoder: DvcDecoder | None = None) -> None:
        self.fb = framebuffer
        self.decoder = decoder

    def set_cipher(self, cipher: int) -> None:
        """Called when the header/encryption command selects a stream cipher.

        Args:
            cipher: Cipher type (0=none, 1=RC4, 2=AES128, 3=AES256)

        This is the critical callback for console channel decryption. The
        keystream initialization should happen here based on the cipher type.
        """

    def send_ack(self) -> None:
        """Called when the decoder needs the console channel to send an ack.

        Use this to acknowledge received commands or status updates.
        """

    def refresh_screen(self) -> None:
        """Called when the decoder detects a stuck/latched state and wants
        a full redraw.

        Trigger a complete screen refresh, typically by re-reading display
        memory or clearing and redrawing known content.
        """

    def on_power(self, on: bool) -> None:
        """Called when the server's power state changes.

        Args:
            on: True if powered on, False if powered off
        """

    def on_health(self, level: int) -> None:
        """Called when the server's health/status LED level changes.

        Args:
            level: Health level code from firmware (typically 0-3)
        """

    def on_licensed(self, flags: int) -> None:
        """Called with the licensing flags reported by the firmware.

        Args:
            flags: Bitmask of license features enabled
        """

    def on_flags(self, flags: int) -> None:
        """Called with miscellaneous firmware feature flags.

        Args:
            flags: Bitmask of optional features supported by firmware
        """

    def on_framerate(self, fps: int) -> None:
        """Called when the firmware reports its target frame rate.

        Args:
            fps: Target frames per second (typically 30 or 60)
        """

    def on_status(self, field: int, text: str) -> None:
        """Called with a status-line update for the given field.

        Args:
            field: Status field index (1-4 typically)
            text: Status text to display in that field
        """

    def on_text(self, text: str) -> None:
        """Called with on-screen text decoded from the print channel.

        Args:
            text: Raw text string from the print channel
        """

    def on_resize(self, width: int, height: int) -> None:
        """Called when the video mode changes to a new resolution.

        Args:
            width: New screen width in pixels
            height: New screen height in pixels
        """

    def on_frame(self) -> None:
        """Called when a full frame has been decoded and is ready to display.

        This is invoked periodically during normal decoding, not just at
        frame boundaries. Use it to trigger periodic screen updates.
        """

    def on_seize(self) -> None:
        """Called when another client seizes control of the console.

        Handle takeover events by notifying your UI or logging the seizure.
        """

    def on_exit(self) -> None:
        """Called when the decoder sees the stream-exit command.

        Clean up resources and stop decoding when the stream ends.
        """


class DvcDecoder:
    """Stateful DVC bit-stream decoder for iLO 3 console video protocol.

    This decoder implements a 48-state finite state machine that processes
    the compressed DVC bit-stream, handling run-length encoding, block-repeat
    coding, colour cache management, and embedded side-band commands.

    Not thread-safe; feed bytes from a single thread only.

    Attributes:
        fb: Framebuffer instance for decoded pixel blocks
        ctl: Controller instance for side-band events
        state: Current FSM state (indices into transition tables)
        bits_to_read: Per-state bit count lookup table
        next_1: State transition table for bit=1 (next_0 is implicit)
        ib_acc: Bit accumulator for partial bytes
        ib_bcnt: Number of bits accumulated
        zero_count: Consecutive zero run counter (for RLE detection)
        block: Current pixel block being filled (256-pixel buffer)
        pixel_count: Pixels written to current block
        lastx/lasty: Last decoded block position
        newx/newy: Next block position from move commands
        size_x/size_y: Target screen dimensions in blocks
        cc_active/cc_color/cc_usage/cc_block: Colour LRU cache state
        color_remap: 5-bit colour palette (32768 entries)

    Args:
        framebuffer: Framebuffer instance to receive decoded pixels
        controller: DvcController instance for side-band events

    Example:
        >>> fb = Framebuffer(width=1024, height=768)
        >>> controller = MyController()
        >>> decoder = DvcDecoder(fb, controller)
        >>> for byte in decrypted_stream:
        ...     result = decoder.feed(byte)
        ...     if result == 1:
        ...         print("Stream ended")
    """

    def __init__(self, framebuffer: Framebuffer, controller: DvcController) -> None:
        """Initialize the decoder with a framebuffer and controller.

        Args:
            framebuffer: Framebuffer instance to receive decoded pixel blocks
            controller: DvcController instance for side-band event callbacks
        """
        self.fb = framebuffer
        self.ctl = controller

        # Copy lookup tables (per-instance state)
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
        """Precompute bit-reversal and zero-run lookup tables for 8-bit bytes.

        These tables are used to efficiently extract variable-length codes from
        the bit stream and detect long runs of zeros (RLE).
        """
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
        """Accumulate a byte into the bit buffer.

        Args:
            c: Input byte (0-255)

        Returns:
            4 if too many consecutive zeros detected (→ HUNT state), else 0
        """
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
        """Extract n bits from the accumulator into ``code``.

        Args:
            n: Number of bits to extract (1-8)
        """
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
        """Clear the colour LRU cache."""
        self.cc_active = 0

    def _pixcode_for(self, active: int) -> int:
        """Determine the next pixcode based on cache activity level.

        Args:
            active: Number of active colours in cache

        Returns:
            Next pixcode state (LATCHED, PIXLRU0, PIXCODE1-4)
        """
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
        """Add a colour to the LRU cache or find an existing entry.

        Args:
            color: Colour value (5-bit index into palette)

        Returns:
            1 if colour was found in cache, 0 if added as new entry
        """
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
        """Find a colour by its LRU usage index.

        Args:
            index: Usage level to search for (0 = most recently used)

        Returns:
            Colour value if found, -1 otherwise
        """
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
        """Remove inactive colours from the cache.

        Removes entries with cc_block == 0, compacting the cache and
        updating usage counters.
        """
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
        """Build the colour remap table for a given bits-per-color.

        Args:
            bpc: Bits per color (2-5)

        The table maps 5-bit indices to 16-bit RGB values.
        """
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
        """Set bits per color from command buffer.

        Args:
            n: Command argument (actual bpc = 5 - (n & 3))
        """
        self.bits_per_color = 5 - (n & 3)
        for s in (PIXGREY, PIXRGBR, PIXRGBG, PIXRGBB):
            self.bits_to_read[s] = self.bits_per_color
        self._build_pixel_table(self.bits_per_color)

    def _set_half_height(self):
        """Detect and switch to half-height mode if needed.

        Half-height (8px blocks) is used for very wide resolutions (>1616px).
        """
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
        """Fill and paste the next ``count`` blocks.

        Args:
            count: Number of blocks to paste (typically 1)
        """
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
        """Process one decrypted stream byte.

        Args:
            byte: Decrypted plaintext byte from the DVC stream

        Returns:
            0: Continue processing
            1: Stream ended (EXIT command received)
        """
        return self._process_bits(byte & 0xFF)

    def _push_pixel(self, value: int) -> bool:
        """Append one pixel to the current block.

        Args:
            value: Pixel colour value (from colour remap table)

        Returns:
            True if successful, False if block overflowed (→ LATCHED state)
        """
        if self.pixel_count >= self.block_height * self.block_width:
            self.next_state = LATCHED
            return False
        self.block[self.pixel_count] = value
        self.pixel_count += 1
        return True

    def _process_bits(self, c: int) -> int:
        """Main decoding loop for a single input byte.

        Args:
            c: Input byte (masked to 8 bits)

        Returns:
            0: Continue processing
            1: Stream ended
            6: Internal error (hung state)
        """
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
        """Execute the action for the current FSM state.

        This is the main state machine dispatch that handles all pixel output,
        colour cache operations, block movement, and command processing.

        Args:
            st: Current state index (0-47)

        Returns:
            True if state transition succeeded, False if hung/detected error
        """
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
        """Complete RGB/grey pixel composition and cache it.

        Handles both RGB (red, green, blue) and grey mode pixels.
        Updates the colour LRU cache if the new colour is different.

        Returns:
            True if successful, False if LATCHED state triggered
        """
        self.blue = self.code
        color = self.red | self.green | self.blue
        if self._cache_lru(color) != 0:
            self.next_state = LATCHED
            return True
        self.last_color = self.color_remap[color]
        self._push_pixel(self.last_color)
        return True

    def _dispatch_command(self):
        """Process a side-band command from the firmware.

        Handles commands 1-16 and 128:
        - 1: Exit stream
        - 2: Print channel start
        - 3: Report framerate
        - 4/5: Power on/off (5 clears screen)
        - 6: Clear status line + frame complete
        - 7: Terminal services (unused headless)
        - 9: Resync bits
        - 10: Seize event
        - 11/13: Set bits per color
        - 12/13: Set video decryption cipher
        - 16: Send ACK
        - 128: Frame complete notification
        """
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
        """Set the video stream decryption cipher.

        Args:
            cipher: Cipher type (0=none, 1=RC4, 2=AES128, 3=AES256)

        The controller wires up the keystream for this cipher type.
        Invalid cipher values default to none.
        """
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
