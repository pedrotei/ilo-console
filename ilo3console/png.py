"""Minimal, dependency-free PNG encoder for streaming framebuffers.

The bridge encodes each decoded video frame as a truecolour PNG (filter type 0,
zlib-compressed) and ships it to the browser. Uses only the standard library so
the core has no image dependency.
"""

from __future__ import annotations

import struct
import zlib
from typing import Protocol


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def encode_png(width: int, height: int, rgb: bytes, *, level: int = 6) -> bytes:
    """Encode packed RGB (3 bytes/pixel, row-major) as an 8-bit truecolour PNG."""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"rgb is {len(rgb)} bytes, expected {expected}")
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # per-scanline filter type: None
        raw += rgb[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )


class _FrameLike(Protocol):
    width: int
    height: int
    pixels: list


def snapshot_png(fb: _FrameLike) -> bytes | None:
    """Encode a framebuffer (0x00RRGGBB int pixels) as PNG.

    Returns ``None`` if the framebuffer is empty or mid-resize (pixel count
    doesn't match the dimensions), so callers can just skip that frame.
    """
    width, height, pixels = fb.width, fb.height, fb.pixels
    if width == 0 or height == 0 or len(pixels) != width * height:
        return None
    rgb = bytearray(width * height * 3)
    for i, px in enumerate(pixels):
        rgb[i * 3] = (px >> 16) & 0xFF
        rgb[i * 3 + 1] = (px >> 8) & 0xFF
        rgb[i * 3 + 2] = px & 0xFF
    return encode_png(width, height, bytes(rgb))
