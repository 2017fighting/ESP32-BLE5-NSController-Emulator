"""The sealing seam (§8.2, §6.4): pure, no device and no HTTP, so key bytes
never cross a request boundary (ADR-0012).

The value types, the 540-byte intake rule and the identity helpers are here;
the amiitool round trip is issue #32's.
"""

from __future__ import annotations

from .api import (
    BCC0_MANUFACTURER_XOR,
    IDENTITY_BLOCK_LENGTH,
    IDENTITY_LENGTH,
    KEY_LENGTH,
    TAG_IMAGE_LENGTH,
    KeyInvalid,
    KeyMaterial,
    SealedTag,
    SealingUnavailable,
    bcc0_for,
    bcc1_for,
    identity_block,
    identity_block_for,
    identity_of,
    mint_identity,
    seal,
    slice_tag_image,
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
