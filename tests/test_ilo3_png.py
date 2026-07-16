"""Structural tests for the PNG encoder (no external image library available)."""

import struct
import zlib

import pytest

from ilo3console.png import PNG_SIGNATURE, encode_png


def _iter_chunks(data):
    assert data[:8] == PNG_SIGNATURE
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF
        yield tag, payload
        pos += 12 + length


def test_png_structure_and_roundtrip():
    width, height = 3, 2
    rgb = bytes(range(width * height * 3))
    png = encode_png(width, height, rgb)
    chunks = list(_iter_chunks(png))
    tags = [t for t, _ in chunks]
    assert tags == [b"IHDR", b"IDAT", b"IEND"]

    w, h, depth, ctype = struct.unpack(">IIBB", chunks[0][1][:10])
    assert (w, h, depth, ctype) == (3, 2, 8, 2)  # 8-bit truecolour RGB

    raw = zlib.decompress(chunks[1][1])
    # Each scanline is prefixed with a filter byte (0), then width*3 RGB bytes.
    stride = width * 3
    recovered = bytearray()
    for y in range(height):
        row = raw[y * (stride + 1) : (y + 1) * (stride + 1)]
        assert row[0] == 0
        recovered += row[1:]
    assert bytes(recovered) == rgb


def test_png_rejects_wrong_length():
    with pytest.raises(ValueError):
        encode_png(2, 2, b"\x00\x00\x00")
