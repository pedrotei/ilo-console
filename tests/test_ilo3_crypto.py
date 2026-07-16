"""Known-answer tests for the console-channel keystream ciphers.

These pin the ports in :mod:`ilo3console.crypto` to published test vectors so a
regression in the byte ordering (which would silently desync the live channel)
is caught immediately.
"""

from ilo3console.crypto import (
    CIPHER_AES128,
    CIPHER_NONE,
    CIPHER_RC4,
    AesOfbKeystream,
    RC4Keystream,
    make_keystream,
)


def _stream(gen, n):
    return bytes(gen.random_value() for _ in range(n))


def test_rc4_rfc6229_128bit_key():
    # RFC 6229, key 0102030405060708090a0b0c0d0e0f10, keystream offset 0.
    key = bytes(range(1, 17))
    expected = bytes.fromhex("9ac7cc9a609d1ef7b2932899cde41b97")
    assert _stream(RC4Keystream(key), 16) == expected


def test_rc4_update_key_resets_stream():
    key = bytes(range(1, 17))
    gen = RC4Keystream(key)
    first = _stream(gen, 8)
    gen.update_key()
    assert _stream(gen, 8) == first


def test_aes128_ofb_zero_key_zero_iv():
    # AES-128 ECB of the all-zero block under the all-zero key is the first
    # OFB keystream block (IV=0).
    key = b"\x00" * 16
    expected = bytes.fromhex("66e94bd4ef8a2c3b884cfa59ca342b2e")
    assert _stream(AesOfbKeystream(0, key), 16) == expected


def test_aes128_ofb_second_block_feeds_forward():
    # Block 2 must be AES(block 1): OFB feedback, not a fresh zero block.
    key = b"\x00" * 16
    b1 = bytes.fromhex("66e94bd4ef8a2c3b884cfa59ca342b2e")
    expected_b2 = _aes_ecb(key, b1)
    got = _stream(AesOfbKeystream(0, key), 32)[16:]
    assert got == expected_b2


def test_aes256_accepts_32_byte_key():
    key = bytes(range(32))
    # Just exercise the path; length/selection must not raise.
    assert len(_stream(AesOfbKeystream(2, key), 48)) == 48


def test_make_keystream_dispatch():
    key = bytes(range(1, 17))
    assert make_keystream(CIPHER_NONE, key) is None
    assert _stream(make_keystream(CIPHER_RC4, key), 4) == _stream(
        RC4Keystream(key), 4
    )
    assert _stream(make_keystream(CIPHER_AES128, key), 4) == _stream(
        AesOfbKeystream(0, key), 4
    )


def _aes_ecb(key, block):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block)
