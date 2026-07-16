"""In-protocol stream ciphers used by the iLO 3 remote console channel.

The console channel is plain TCP. Once the firmware sends the ``ESC [ R``
sequence, every subsequent byte in *both* directions is XORed with a keystream
byte. The firmware chooses which cipher via the DVC ``set_encryption`` command
(see :mod:`ilo3console.dvc`): RC4-128, AES-128 or AES-256. All three are used
purely as keystream generators — the transport never encrypts blocks directly,
it only pulls one keystream byte per payload byte and XORs.

Both generators are byte-for-byte matches of the firmware's own keystreams
(see docs/PROTOCOL_ILO3.md's "In-protocol encryption" section); this matters
because the two ends must stay bit-synchronised for the lifetime of the
connection.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class RC4Keystream:
    """Standard RC4 PRGA keyed with a 16-byte key.

    The key schedule always consumes exactly 16 key bytes (``key[n % 16]``),
    and :meth:`random_value` returns one keystream byte at a time.
    :meth:`update_key` re-runs the key schedule from the original key,
    resetting the stream — the firmware triggers this on a key-change
    command.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("RC4 key must be at least 16 bytes")
        self._pre = bytes(key[:16])
        self._sbox = bytearray(256)
        self._i = 0
        self._j = 0
        self.update_key()

    def update_key(self) -> None:
        """Re-run the RC4 key schedule from the original key, resetting the stream."""
        sbox = self._sbox
        key = self._pre
        for n in range(256):
            sbox[n] = n
        j = 0
        for n in range(256):
            j = (j + sbox[n] + key[n % 16]) & 0xFF
            sbox[n], sbox[j] = sbox[j], sbox[n]
        self._i = 0
        self._j = 0

    def random_value(self) -> int:
        """Advance the RC4 PRGA state and return the next keystream byte."""
        sbox = self._sbox
        self._i = (self._i + 1) & 0xFF
        self._j = (self._j + sbox[self._i]) & 0xFF
        i, j = self._i, self._j
        sbox[i], sbox[j] = sbox[j], sbox[i]
        return sbox[(sbox[i] + sbox[j]) & 0xFF]


class AesOfbKeystream:
    """AES keystream in OFB mode with an all-zero IV.

    The firmware constructs the cipher with a zeroed 16-byte feedback
    block, and each keystream block is ``AES-ECB(key, previous_block)``
    starting from zeros — i.e. OFB with IV=0. :meth:`random_value` streams
    the blocks out one byte at a time.

    ``bits`` selects the key size: ``0`` -> AES-128, ``2`` -> AES-256.
    (192-bit, ``1``, is a valid value but never selected by the protocol.)
    """

    _KEY_LEN = {0: 16, 1: 24, 2: 32}

    def __init__(self, bits: int, key: bytes) -> None:
        try:
            key_len = self._KEY_LEN[bits]
        except KeyError as exc:
            raise ValueError(f"invalid AES bits selector: {bits}") from exc
        if len(key) < key_len:
            raise ValueError(f"AES-{key_len * 8} needs {key_len} key bytes")
        self._encryptor = Cipher(
            algorithms.AES(bytes(key[:key_len])), modes.ECB()
        ).encryptor()
        self._block = b"\x00" * 16
        self._pos = 0

    def random_value(self) -> int:
        """Return the next keystream byte, encrypting a new AES-OFB block
        every 16 bytes.
        """
        if self._pos == 0:
            self._block = self._encryptor.update(self._block)
        byte = self._block[self._pos]
        self._pos = (self._pos + 1) & 0x0F
        return byte


# Cipher selectors as sent by the firmware's DVC set_encryption command.
CIPHER_NONE = 0
CIPHER_RC4 = 1
CIPHER_AES128 = 2
CIPHER_AES256 = 3


def make_keystream(cipher: int, key: bytes):
    """Build the keystream generator for a firmware cipher selector.

    Returns ``None`` for :data:`CIPHER_NONE`. The returned object always exposes
    ``random_value() -> int``.
    """
    if cipher == CIPHER_NONE:
        return None
    if cipher == CIPHER_RC4:
        return RC4Keystream(key)
    if cipher == CIPHER_AES128:
        return AesOfbKeystream(0, key)
    if cipher == CIPHER_AES256:
        return AesOfbKeystream(2, key)
    raise ValueError(f"unknown cipher selector: {cipher}")
