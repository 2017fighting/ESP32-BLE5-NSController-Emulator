"""The sealing seam: a pure function from a raw tag image and key to a Tag (§8.2, §6.4).

```text
seal(figure's raw 540 B dump, key material) -> sealed 540 B Tag + minted Identity
```

(That line is §6.4's, quoted; elsewhere here the 540 bytes are a **tag image**,
which is what `CONTEXT.md` calls the artifact.)

It has no device and no HTTP access, so key bytes never reach the framing layer
or a request handler (ADR-0012). Issue #28 settled the flow against amiitool
(`docs/research/amiitool-sealing-port.md`); #32 implements it, and the oracle
for these bytes is amiitool's own output in `fixtures/sealing/`.

**Two coordinate systems, and each one's offsets are the ones that belong to
it** (§6.3):

| Region | Tag image (540 B) | Internal cache (520 B) |
| --- | --- | --- |
| identity block (the KDF seed) | `0x000` | `0x1D4` |
| tag HMAC | `0x034` | `0x1B4` |
| data HMAC | `0x080` | `0x008` |

`unpack` converts to the cache's layout and verifies **both** HMACs; `pack`
mints (or is given) an identity, recomputes `BCC0`/`BCC1` and re-seals. The
cache carries the *source* tag's identity at `0x1D4`, which is why `pack` copies
it and never writes in place — sealing the cache in place would corrupt it
(§6.4).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from .crypto import (
    KEY_LENGTH,
    AmiiboKeys,
    DerivedKeys,
    MasterKey,
    MasterKeyError,
    aes128_ctr,
    derive_keys,
    hmac_sha256,
)

#: The tag image: 135 pages × 4 bytes (§6.2).
TAG_IMAGE_LENGTH = 540
#: `NFC3D_AMIIBO_SIZE`: 130 pages, the only part an internal layout covers.
INTERNAL_LENGTH = 520
#: Pages 130–134: dynamic lock bits, `CFG0`, `CFG1`, password, `PACK`.
TRAILER_LENGTH = 20
IDENTITY_LENGTH = 7
IDENTITY_BLOCK_LENGTH = 8
BCC0_MANUFACTURER_XOR = 0x88

# Internal coordinates (`nfc3d_amiibo_tag_to_internal`).
INTERNAL_IDENTITY_OFFSET = 0x1D4
INTERNAL_TAG_HMAC_OFFSET = 0x1B4
INTERNAL_DATA_HMAC_OFFSET = 0x008
# Tag-image coordinates (`nfc3d_amiibo_internal_to_tag`).
TAG_IDENTITY_OFFSET = 0x000
TAG_TAG_HMAC_OFFSET = 0x034
TAG_DATA_HMAC_OFFSET = 0x080
# The AES-128-CTR span: internal `0x02C`–`0x1B3`, 0x188 bytes.
CIPHERTEXT_OFFSET = 0x02C
CIPHERTEXT_LENGTH = 0x188
# The plaintext the tag HMAC covers: the identity block and the settings tail.
TAG_HMAC_SPAN = (INTERNAL_IDENTITY_OFFSET, 0x208)
# The plaintext the data HMAC covers, as one run in internal coordinates.
DATA_HMAC_SPAN = (0x029, 0x208)

#: The pass-throughs `nfc3d_amiibo_cipher` copies rather than encrypting.
_CIPHER_PASSTHROUGHS = ((0x000, 0x008), (0x028, 0x02C), (0x1D4, 0x208))


class KeyInvalid(ValueError):
    """The mounted key material is not a usable retail key (§6.7)."""


class TagInvalid(ValueError):
    """A tag image's signatures do not verify under this key.

    Both HMACs are checked (§6.7's rung 3 is *both*, not either), so this is
    also what a wrong-but-structurally-valid key produces.
    """


# ── identity ───────────────────────────────────────────────────────────────


def slice_tag_image(data: bytes) -> bytes:
    """The first 540 bytes, never trusting the file size (§6.2).

    Eleven library `.bin` files are 572 bytes (540 + a 32-byte trailer that is
    not a hash of the 540), so a reader that trusts the size silently mis-reads
    them.
    """
    if len(data) < TAG_IMAGE_LENGTH:
        raise ValueError(f"tag image is {len(data)} bytes, shorter than {TAG_IMAGE_LENGTH}")
    return data[:TAG_IMAGE_LENGTH]


def _uid_from_block(block: bytes) -> bytes:
    """`UID[0..2] || UID[3..6]`: the seven bytes around the `BCC0` check byte."""
    return bytes(block[0:3]) + bytes(block[4:8])


def identity_of(tag_image: bytes) -> bytes:
    """The seven-byte UID out of the tag image (§6.3)."""
    if len(tag_image) < IDENTITY_BLOCK_LENGTH:
        raise ValueError("tag image is shorter than its identity block")
    return _uid_from_block(tag_image)


def identity_block(tag_image: bytes) -> bytes:
    """The 8-byte KDF seed region at offset `0x000`."""
    if len(tag_image) < IDENTITY_BLOCK_LENGTH:
        raise ValueError("tag image is shorter than its identity block")
    return bytes(tag_image[0:IDENTITY_BLOCK_LENGTH])


def mint_identity() -> bytes:
    """A fresh `0x04` (NXP) + 6 CSPRNG bytes (§6.3).

    Identities are never recorded and never reused deliberately; the space is
    2^48 and no store of issued identities exists.
    """
    return bytes([0x04]) + secrets.token_bytes(6)


def identity_block_for(identity: bytes) -> bytes:
    """The 8-byte KDF seed region for `identity`: `UID[0..2]`, `BCC0`, `UID[3..6]`."""
    if len(identity) != IDENTITY_LENGTH:
        raise ValueError(f"identity is {len(identity)} bytes, expected {IDENTITY_LENGTH}")
    return identity[:3] + bytes([bcc0_for(identity)]) + identity[3:]


def bcc0_for(identity: bytes) -> int:
    """`BCC0 = 0x88 ^ UID[0] ^ UID[1] ^ UID[2]` (§6.3)."""
    return BCC0_MANUFACTURER_XOR ^ identity[0] ^ identity[1] ^ identity[2]


def bcc1_for(identity: bytes) -> int:
    """`BCC1 = UID[3] ^ UID[4] ^ UID[5] ^ UID[6]` (§6.3)."""
    return identity[3] ^ identity[4] ^ identity[5] ^ identity[6]


# ── the value types ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """160 bytes of the user's own key, held read-only in memory (ADR-0012).

    `spelling` is `"single file (key_retail.bin)"` or the two-file form, purely
    so the startup line and the Settings screen can say which was read — never
    the bytes. Construction runs §6.7's rungs 1–2 (length, masterkey structure);
    rung 3 is the round trip and belongs to the caller, because it needs a tag.

    The bytes are kept out of `repr`: a key that reaches a traceback, a log line
    or an error message has left the process.
    """

    data: bytes = field(repr=False)
    spelling: str = "single file"
    keys: AmiiboKeys = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            parsed = AmiiboKeys.parse(self.data)
        except MasterKeyError as invalid:
            raise KeyInvalid(str(invalid)) from None
        object.__setattr__(self, "keys", parsed)

    @property
    def data_master(self) -> MasterKey:
        """The unfixed-infos masterkey, which derives the data keys (§6.7)."""
        return self.keys.data

    @property
    def tag_master(self) -> MasterKey:
        """The locked-secret masterkey, which derives the tag keys (§6.7)."""
        return self.keys.tag


@dataclass(frozen=True, slots=True)
class SealedTag:
    """A sealed 540-byte tag image and the identity minted into it."""

    image: bytes
    identity: bytes

    def __post_init__(self) -> None:
        if len(self.image) != TAG_IMAGE_LENGTH:
            raise ValueError(f"tag image is {len(self.image)} bytes")
        if len(self.identity) != IDENTITY_LENGTH:
            raise ValueError(f"identity is {len(self.identity)} bytes")


@dataclass(frozen=True, slots=True)
class Plaintext:
    """The 520-byte cache in internal coordinates, plus the verbatim trailer.

    `identity` reads the cache's *own* identity at `0x1D4` — the source tag's,
    unless `with_identity` has replaced it.
    """

    internal: bytes
    trailer: bytes = bytes(TRAILER_LENGTH)

    def __post_init__(self) -> None:
        if len(self.internal) != INTERNAL_LENGTH:
            raise ValueError(f"plaintext is {len(self.internal)} bytes, expected {INTERNAL_LENGTH}")
        if len(self.trailer) != TRAILER_LENGTH:
            raise ValueError(f"trailer is {len(self.trailer)} bytes, expected {TRAILER_LENGTH}")

    @property
    def identity(self) -> bytes:
        block = self.internal[INTERNAL_IDENTITY_OFFSET : INTERNAL_IDENTITY_OFFSET + IDENTITY_BLOCK_LENGTH]
        return _uid_from_block(block)

    def with_identity(self, identity: bytes) -> Plaintext:
        """A **copy** carrying `identity`: `BCC0` at `0x1D7`, `BCC1` at `0x000`.

        A copy because the cache must survive rotation (§6.4): writing in place
        would leave the source tag's identity nowhere and destroy the seam that
        makes the next rotation possible.
        """
        internal = bytearray(self.internal)
        internal[INTERNAL_IDENTITY_OFFSET : INTERNAL_IDENTITY_OFFSET + IDENTITY_BLOCK_LENGTH] = identity_block_for(identity)
        internal[0] = bcc1_for(identity)
        return Plaintext(internal=bytes(internal), trailer=self.trailer)


# ── the round trip ─────────────────────────────────────────────────────────


def _tag_to_internal(tag: bytes) -> bytes:
    """`nfc3d_amiibo_tag_to_internal` (`amiitool/amiibo.c:53-61`)."""
    internal = bytearray(INTERNAL_LENGTH)
    internal[0x000:0x008] = tag[0x008:0x010]
    internal[0x008:0x028] = tag[0x080:0x0A0]
    internal[0x028:0x04C] = tag[0x010:0x034]
    internal[0x04C:0x1B4] = tag[0x0A0:0x208]
    internal[0x1B4:0x1D4] = tag[0x034:0x054]
    internal[0x1D4:0x1DC] = tag[0x000:0x008]
    internal[0x1DC:0x208] = tag[0x054:0x080]
    return bytes(internal)


def _internal_to_tag(internal: bytes) -> bytes:
    """`nfc3d_amiibo_internal_to_tag` (`amiitool/amiibo.c:63-71`)."""
    tag = bytearray(INTERNAL_LENGTH)
    tag[0x008:0x010] = internal[0x000:0x008]
    tag[0x080:0x0A0] = internal[0x008:0x028]
    tag[0x010:0x034] = internal[0x028:0x04C]
    tag[0x0A0:0x208] = internal[0x04C:0x1B4]
    tag[0x034:0x054] = internal[0x1B4:0x1D4]
    tag[0x000:0x008] = internal[0x1D4:0x1DC]
    tag[0x054:0x080] = internal[0x1DC:0x208]
    return bytes(tag)


def _calc_seed(internal: bytes) -> bytes:
    """`nfc3d_amiibo_calc_seed` (`amiitool/amiibo.c:19-25`).

    The identity block is in here twice over, which is what makes a UID swap
    impossible without a re-seal (§6.3).
    """
    seed = bytearray(64)
    seed[0x00:0x02] = internal[0x029:0x02B]
    seed[0x10:0x18] = internal[INTERNAL_IDENTITY_OFFSET : INTERNAL_IDENTITY_OFFSET + 8]
    seed[0x18:0x20] = internal[INTERNAL_IDENTITY_OFFSET : INTERNAL_IDENTITY_OFFSET + 8]
    seed[0x20:0x40] = internal[0x1E8:0x208]
    return bytes(seed)


def _cipher(keys: DerivedKeys, source: bytes) -> bytes:
    """`nfc3d_amiibo_cipher` (`amiitool/amiibo.c:34-51`).

    AES-128-CTR over the 392 encrypted bytes, with the header, the write-counter
    prefix and the identity/tail copied through. The HMAC regions are left zero:
    the caller owns them, because in `unpack` they are recomputed and in `pack`
    the fresh tag HMAC feeds the data HMAC.
    """
    out = bytearray(len(source))
    span = slice(CIPHERTEXT_OFFSET, CIPHERTEXT_OFFSET + CIPHERTEXT_LENGTH)
    out[span] = aes128_ctr(keys.aes_key, keys.aes_iv, source[span])
    for start, end in _CIPHER_PASSTHROUGHS:
        out[start:end] = source[start:end]
    return bytes(out)


def unpack(tag_image: bytes, key: KeyMaterial) -> Plaintext:
    """Verify `tag_image` under `key` and return its 520-byte internal cache.

    **Both** HMACs are required (§6.7): the tag HMAC covers the identity block,
    and the data HMAC covers the tag HMAC, so a tag that passes one and fails
    the other is not a tag this key signed. Raises `TagInvalid` on either
    failure — the caller decides whether that accuses the key or the file.
    """
    if len(tag_image) != TAG_IMAGE_LENGTH:
        raise ValueError(f"tag image is {len(tag_image)} bytes, expected {TAG_IMAGE_LENGTH}")
    internal = _tag_to_internal(tag_image)
    seed = _calc_seed(internal)
    data_keys = derive_keys(key.data_master, seed)
    tag_keys = derive_keys(key.tag_master, seed)

    plain = bytearray(_cipher(data_keys, internal))
    tag_hmac = _tag_signature(tag_keys, plain)
    if not _same(tag_hmac, internal[INTERNAL_TAG_HMAC_OFFSET : INTERNAL_TAG_HMAC_OFFSET + 32]):
        raise TagInvalid("the tag HMAC does not verify under this key")
    plain[INTERNAL_TAG_HMAC_OFFSET : INTERNAL_TAG_HMAC_OFFSET + 32] = tag_hmac

    data_hmac = _data_signature(data_keys, plain)
    if not _same(data_hmac, internal[INTERNAL_DATA_HMAC_OFFSET : INTERNAL_DATA_HMAC_OFFSET + 32]):
        raise TagInvalid("the data HMAC does not verify under this key")
    plain[INTERNAL_DATA_HMAC_OFFSET : INTERNAL_DATA_HMAC_OFFSET + 32] = data_hmac

    return Plaintext(internal=bytes(plain), trailer=tag_image[INTERNAL_LENGTH:TAG_IMAGE_LENGTH])


def pack(plain: Plaintext, key: KeyMaterial, *, identity: bytes | None = None) -> SealedTag:
    """Seal `plain` under `key`, as `identity` or as the cache's own identity.

    `identity=None` keeps the cache's identity, which makes `pack(unpack(tag))`
    the tag again — the property that proves the round trip lost nothing. A
    placement passes a fresh identity, and `seal` is the caller that mints one.
    """
    working = plain if identity is None else plain.with_identity(identity)
    sealed_identity = working.identity

    seed = _calc_seed(working.internal)
    tag_keys = derive_keys(key.tag_master, seed)
    data_keys = derive_keys(key.data_master, seed)

    # Amiitool's order, and its two different buffers: the tag HMAC covers the
    # plaintext's identity block and tail; the data HMAC covers the plaintext
    # payload (not the ciphertext), the *fresh* tag HMAC, and the plaintext
    # tail again.
    signable = bytearray(working.internal)
    tag_hmac = _tag_signature(tag_keys, signable)
    signable[INTERNAL_TAG_HMAC_OFFSET : INTERNAL_TAG_HMAC_OFFSET + 32] = tag_hmac

    cipher = bytearray(_cipher(data_keys, working.internal))
    cipher[INTERNAL_TAG_HMAC_OFFSET : INTERNAL_TAG_HMAC_OFFSET + 32] = tag_hmac
    cipher[INTERNAL_DATA_HMAC_OFFSET : INTERNAL_DATA_HMAC_OFFSET + 32] = _data_signature(data_keys, signable)

    return SealedTag(image=_internal_to_tag(bytes(cipher)) + working.trailer, identity=sealed_identity)


def seal(tag_image: bytes, key: KeyMaterial, *, identity: bytes | None = None) -> SealedTag:
    """Seal a figure's raw 540 B tag image under `key` with a fresh (or given) identity.

    Pure: no device, no HTTP, no I/O. **Per placement, never precomputed**
    (§6.4): a variant set would repeat identities, which a game can see, and
    cost 540 B of device RAM each to save the ~7 ms push.
    """
    plain = unpack(tag_image, key)
    return pack(plain, key, identity=mint_identity() if identity is None else identity)


def _tag_signature(keys: DerivedKeys, plain: bytes | bytearray) -> bytes:
    return hmac_sha256(keys.hmac_key, bytes(plain[TAG_HMAC_SPAN[0] : TAG_HMAC_SPAN[1]]))


def _data_signature(keys: DerivedKeys, plain: bytes | bytearray) -> bytes:
    return hmac_sha256(keys.hmac_key, bytes(plain[DATA_HMAC_SPAN[0] : DATA_HMAC_SPAN[1]]))


def _same(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)


class Sealer:
    """`seal`, with §6.4's lazy plaintext cache.

    ```text
    unpack(tag) -> 520 B internal cache, kept in process memory only
    pack(cache.copy().with_identity(minted)) -> 540 B Tag
    ```

    The cache is keyed by the source tag image and is dropped when the key it was
    made under changes (the key is read once, so that is a restart or a test).
    ~494 KB worst case for the whole 951-figure library, which §6.4 accepts
    against re-unpacking per placement. It is decrypted save data and Mii
    content: never on disk, and never mutated.
    """

    def __init__(self) -> None:
        self._plaintexts: dict[bytes, Plaintext] = {}
        self._key_fingerprint: bytes | None = None
        self.unpacks = 0

    @property
    def cached(self) -> int:
        return len(self._plaintexts)

    def plaintext(self, tag_image: bytes, key: KeyMaterial) -> Plaintext:
        fingerprint = hashlib.sha256(key.data).digest()
        if fingerprint != self._key_fingerprint:
            self._plaintexts.clear()
            self._key_fingerprint = fingerprint
        cache_key = hashlib.sha256(tag_image).digest()
        cached = self._plaintexts.get(cache_key)
        if cached is None:
            self.unpacks += 1
            cached = unpack(tag_image, key)
            self._plaintexts[cache_key] = cached
        return cached

    def __call__(
        self, tag_image: bytes, key: KeyMaterial, *, identity: bytes | None = None
    ) -> SealedTag:
        """The seam's signature (§8.2): a raw tag image and key in, a Tag and identity out.

        One identity per placement (§6.1), which is why this mints rather than
        reusing the cache's: the cache keeps the *source* tag's identity at
        `0x1D4`, and replaying it is the failure mode §6.7 bans. `identity` pins
        the seal to a given UID — the bench's continue-for-write re-presentation
        (§6.6), never a production path (ADR-0011).
        """
        return pack(
            self.plaintext(tag_image, key),
            key,
            identity=mint_identity() if identity is None else identity,
        )


__all__ = [
    "BCC0_MANUFACTURER_XOR",
    "CIPHERTEXT_LENGTH",
    "CIPHERTEXT_OFFSET",
    "IDENTITY_BLOCK_LENGTH",
    "IDENTITY_LENGTH",
    "INTERNAL_DATA_HMAC_OFFSET",
    "INTERNAL_IDENTITY_OFFSET",
    "INTERNAL_LENGTH",
    "INTERNAL_TAG_HMAC_OFFSET",
    "KEY_LENGTH",
    "TAG_DATA_HMAC_OFFSET",
    "TAG_IDENTITY_OFFSET",
    "TAG_IMAGE_LENGTH",
    "TAG_TAG_HMAC_OFFSET",
    "TRAILER_LENGTH",
    "KeyInvalid",
    "KeyMaterial",
    "Plaintext",
    "SealedTag",
    "Sealer",
    "TagInvalid",
    "bcc0_for",
    "bcc1_for",
    "identity_block",
    "identity_block_for",
    "identity_of",
    "mint_identity",
    "pack",
    "seal",
    "slice_tag_image",
    "unpack",
]
