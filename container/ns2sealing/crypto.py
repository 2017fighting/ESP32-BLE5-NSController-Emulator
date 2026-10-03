"""The amiitool cryptographic kernel: masterkeys, key derivation, HMAC, AES-CTR.

A faithful port of `socram8888/amiitool` at
`4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3` (the pin in `docs/references.md`):
`keygen.c`'s `nfc3d_keygen_prepare_seed`/`nfc3d_keygen`, `drbg.c`'s
HMAC-SHA256 DRBG, and the AES-128-CTR and HMAC-SHA256 calls in `amiibo.c`. The
byte-level flow that uses it is `api.py`; the port's evidence is
`docs/research/amiitool-sealing-port.md` §2–§3 and the frozen oracle in
`fixtures/sealing/`.

Nothing here knows about tags, identities or the container: it is the crypto
kernel and its two `typeString` labels, so the port stays comparable with the C
it was ported from.
"""

from __future__ import annotations

import hashlib
import hmac as hmaclib
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: `nfc3d_keygen_masterkeys`: hmacKey[16] || typeString[14] || rfu || magicBytesSize
#: || magicBytes[16] || xorPad[32] (`amiitool/include/nfc3d/keygen.h`).
MASTERKEY_LENGTH = 80
#: `nfc3d_amiibo_keys`: the unfixed-infos masterkey then the locked-secret one.
KEY_LENGTH = 160
TYPE_STRING_LENGTH = 14
MAGIC_BYTES_LENGTH = 16
MAGIC_BYTES_MAX = 16
HMAC_KEY_LENGTH = 16
AES_KEY_LENGTH = 16
AES_IV_LENGTH = 16
#: `NFC3D_KEYGEN_SEED_SIZE`.
SEED_LENGTH = 64
#: `NFC3D_DRBG_OUTPUT_SIZE`: one DRBG iteration is one SHA-256 HMAC.
DRBG_OUTPUT_SIZE = 32
#: aesKey || aesIV || hmacKey, the whole `nfc3d_keygen_derivedkeys`.
DERIVED_LENGTH = 48

_TYPE_STRING_OFFSET = HMAC_KEY_LENGTH
_RFU_OFFSET = _TYPE_STRING_OFFSET + TYPE_STRING_LENGTH
_MAGIC_BYTES_SIZE_OFFSET = _RFU_OFFSET + 1

#: amiitool's own names for the two key sets (`amiitool/README.md`: "the key is
#: the concatenation of unfixed infos and locked secret keys"). These are the
#: format's labels, part of the KDF seed, not secret material.
UNFIXED_INFOS_LABEL = b"unfixed infos"
LOCKED_SECRET_LABEL = b"locked secret"


class MasterKeyError(ValueError):
    """The key file is not the masterkey structure (§6.7's rung 2)."""


@dataclass(frozen=True, slots=True)
class MasterKey:
    """One 80-byte key set, with its `typeString` parsed out."""

    hmac_key: bytes
    #: The `typeString` **up to and including** its NUL, as `memccpy` copies it.
    type_string: bytes
    magic_bytes: bytes
    xor_pad: bytes


@dataclass(frozen=True, slots=True)
class AmiiboKeys:
    """The 160 bytes of §6.7: the unfixed-infos and locked-secret masterkeys."""

    data: MasterKey
    tag: MasterKey

    @classmethod
    def parse(cls, raw: bytes) -> AmiiboKeys:
        """Rungs 1–2 of §6.7's ladder: the length, then the masterkey structure.

        The `typeString`s are matched against Nintendo's two labels rather than
        merely being checked for printability, because §6.7 names them and
        because a key whose label is something else derives different keys
        anyway — the round trip (rung 3) is the arbiter, and this rung exists so
        a random 160-byte file never reaches it. The trade-off: a key set with
        foreign labels is reported invalid rather than unverified, which is the
        honest answer for a file that is not this format.
        """
        if len(raw) != KEY_LENGTH:
            raise MasterKeyError(f"key material is {len(raw)} bytes, expected {KEY_LENGTH}")
        return cls(
            data=_parse_masterkey(raw[:MASTERKEY_LENGTH], UNFIXED_INFOS_LABEL),
            tag=_parse_masterkey(raw[MASTERKEY_LENGTH:], LOCKED_SECRET_LABEL),
        )


def _parse_masterkey(raw: bytes, label: bytes) -> MasterKey:
    if len(raw) != MASTERKEY_LENGTH:
        raise MasterKeyError(f"masterkey is {len(raw)} bytes, expected {MASTERKEY_LENGTH}")
    type_field = raw[_TYPE_STRING_OFFSET : _TYPE_STRING_OFFSET + TYPE_STRING_LENGTH]
    # `memccpy` copies through the NUL and leaves the rest alone; Nintendo's own
    # files zero-pad, so requiring that is a stronger rung-2 check than trusting it.
    expected = label + b"\x00"
    if type_field != expected + bytes(TYPE_STRING_LENGTH - len(expected)):
        raise MasterKeyError(f"masterkey typeString is not {label!r}")
    if raw[_RFU_OFFSET] != 0:
        raise MasterKeyError(f"masterkey rfu is {raw[_RFU_OFFSET]}, expected 0")
    size = raw[_MAGIC_BYTES_SIZE_OFFSET]
    if size > MAGIC_BYTES_MAX:
        raise MasterKeyError(f"masterkey magicBytesSize is {size}, above {MAGIC_BYTES_MAX}")
    return MasterKey(
        hmac_key=raw[:HMAC_KEY_LENGTH],
        type_string=expected,
        magic_bytes=raw[
            _MAGIC_BYTES_SIZE_OFFSET + 1 : _MAGIC_BYTES_SIZE_OFFSET + 1 + size
        ],
        xor_pad=raw[MASTERKEY_LENGTH - 32 :],
    )


@dataclass(frozen=True, slots=True)
class DerivedKeys:
    """`nfc3d_keygen_derivedkeys`: the AES key/IV and the HMAC key for one role."""

    aes_key: bytes
    aes_iv: bytes
    hmac_key: bytes


def prepare_seed(master: MasterKey, base_seed: bytes) -> bytes:
    """`nfc3d_keygen_prepare_seed` (`amiitool/keygen.c:14-46`).

    `typeString` (with its NUL) → leading `16 - magicBytesSize` seed bytes →
    `magicBytes` → seed bytes `0x10`–`0x1F` → seed bytes `0x20`–`0x3F` XOR the
    masterkey's `xorPad`.
    """
    if len(base_seed) != SEED_LENGTH:
        raise ValueError(f"seed is {len(base_seed)} bytes, expected {SEED_LENGTH}")
    leading = MAGIC_BYTES_LENGTH - len(master.magic_bytes)
    return (
        master.type_string
        + base_seed[:leading]
        + master.magic_bytes
        + base_seed[0x10:0x20]
        + bytes(a ^ b for a, b in zip(base_seed[0x20:0x40], master.xor_pad, strict=True))
    )


def drbg_bytes(hmac_key: bytes, seed: bytes, length: int) -> bytes:
    """`nfc3d_drbg_generate_bytes` (`amiitool/drbg.c:59-78`).

    A big-endian 16-bit counter is prepended to the seed, HMAC-SHA256'd under
    the masterkey's `hmacKey`, and each iteration yields 32 bytes.
    """
    if length < 0:
        raise ValueError("length must not be negative")
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac_sha256(hmac_key, counter.to_bytes(2, "big") + seed)
        counter += 1
    return bytes(out[:length])


def derive_keys(master: MasterKey, seed: bytes) -> DerivedKeys:
    """`nfc3d_keygen`: one DRBG stream, cut into AES key, AES IV and HMAC key."""
    material = drbg_bytes(master.hmac_key, prepare_seed(master, seed), DERIVED_LENGTH)
    return DerivedKeys(
        aes_key=material[:AES_KEY_LENGTH],
        aes_iv=material[AES_KEY_LENGTH : AES_KEY_LENGTH + AES_IV_LENGTH],
        hmac_key=material[AES_KEY_LENGTH + AES_IV_LENGTH :],
    )


def aes128_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CTR, which is its own inverse (as `nfc3d_amiibo_cipher` uses it)."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmaclib.new(key, data, hashlib.sha256).digest()


__all__ = [
    "AES_IV_LENGTH",
    "AES_KEY_LENGTH",
    "DERIVED_LENGTH",
    "DRBG_OUTPUT_SIZE",
    "HMAC_KEY_LENGTH",
    "KEY_LENGTH",
    "LOCKED_SECRET_LABEL",
    "MAGIC_BYTES_MAX",
    "MASTERKEY_LENGTH",
    "SEED_LENGTH",
    "TYPE_STRING_LENGTH",
    "UNFIXED_INFOS_LABEL",
    "AmiiboKeys",
    "DerivedKeys",
    "MasterKey",
    "MasterKeyError",
    "aes128_ctr",
    "derive_keys",
    "drbg_bytes",
    "hmac_sha256",
    "prepare_seed",
]
