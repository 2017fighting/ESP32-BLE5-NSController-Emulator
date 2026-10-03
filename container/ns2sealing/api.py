"""The sealing seam: a pure function from a raw tag image and key to a Tag (§8.2, §6.4).

```text
seal(figure's raw 540 B dump, key material) -> sealed 540 B Tag + minted Identity
```

It has no device and no HTTP access, so key bytes never reach the framing layer
or a request handler (ADR-0012). Issue #28 settled the *flow* against amiitool
(`docs/research/amiitool-sealing-port.md`); issue #32 implements it. #29 fixes
the signature, the value types and the byte-coordinate helpers that are already
facts of the format — `identity_of` here is the one both the device stub and
the future sealer must agree on.

The image is 135 pages × 4 bytes. §6.3's tag-image coordinates are the ones
used here: the 8-byte KDF seed region is offsets `0x000`–`0x007`, the seven-byte
UID is `UID[0..2]` then `UID[3..6]` around the `BCC0` check byte at offset 3.
"""

from __future__ import annotations

from dataclasses import dataclass

TAG_IMAGE_LENGTH = 540
IDENTITY_LENGTH = 7
IDENTITY_BLOCK_LENGTH = 8
KEY_LENGTH = 160
BCC0_MANUFACTURER_XOR = 0x88


class SealingUnavailable(RuntimeError):
    """Sealing is not implemented in this build (issue #32 owns it)."""


class KeyInvalid(ValueError):
    """The mounted key material is not a usable retail key (§6.7)."""


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """160 bytes of the user's own key, held read-only in memory (ADR-0012).

    `spelling` is `"single file (key_retail.bin)"` or the two-file form, purely
    so the startup line and the Settings screen can say which was read — never
    the bytes.
    """

    data: bytes
    spelling: str = "single file (key_retail.bin)"

    def __post_init__(self) -> None:
        if len(self.data) != KEY_LENGTH:
            raise KeyInvalid(f"key material is {len(self.data)} bytes, expected {KEY_LENGTH}")


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


def slice_tag_image(data: bytes) -> bytes:
    """The first 540 bytes, never trusting the file size (§6.2).

    Eleven library `.bin` files are 572 bytes (540 + a 32-byte trailer that is
    not a hash of the 540), so a reader that trusts the size silently mis-reads
    them.
    """
    if len(data) < TAG_IMAGE_LENGTH:
        raise ValueError(f"tag image is {len(data)} bytes, shorter than {TAG_IMAGE_LENGTH}")
    return data[:TAG_IMAGE_LENGTH]


def identity_of(tag_image: bytes) -> bytes:
    """The seven-byte UID out of the tag image (§6.3)."""
    if len(tag_image) < IDENTITY_BLOCK_LENGTH:
        raise ValueError("tag image is shorter than its identity block")
    return bytes(tag_image[0:3]) + bytes(tag_image[4:8])


def identity_block(tag_image: bytes) -> bytes:
    """The 8-byte KDF seed region at offset `0x000`."""
    if len(tag_image) < IDENTITY_BLOCK_LENGTH:
        raise ValueError("tag image is shorter than its identity block")
    return bytes(tag_image[0:IDENTITY_BLOCK_LENGTH])


def mint_identity() -> bytes:
    """A fresh `0x04` (NXP) + 6 CSPRNG bytes (§6.3).

    Present so the *shape* of a minted identity is testable now; the sealing
    round trip that writes it into an image is issue #32's.
    """
    import secrets

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


def seal(
    tag_image: bytes,
    key: KeyMaterial,
    *,
    identity: bytes | None = None,
) -> SealedTag:
    """Seal `tag_image` under `key` with a fresh (or supplied) `identity`.

    Pure: no device, no HTTP, no I/O. **Not implemented here** — issue #32
    ports amiitool's flow (`unpack → overwrite identity → pack`, both HMACs)
    against the pinned source; until then every caller must refuse a placement
    locally rather than pretending to seal.
    """
    raise SealingUnavailable(
        "sealing is issue #32's; the seam and its value types are issue #29's"
    )


__all__ = [
    "BCC0_MANUFACTURER_XOR",
    "IDENTITY_BLOCK_LENGTH",
    "IDENTITY_LENGTH",
    "KEY_LENGTH",
    "KeyInvalid",
    "KeyMaterial",
    "SealedTag",
    "SealingUnavailable",
    "TAG_IMAGE_LENGTH",
    "bcc0_for",
    "bcc1_for",
    "identity_block",
    "identity_block_for",
    "identity_of",
    "mint_identity",
    "seal",
    "slice_tag_image",
]
