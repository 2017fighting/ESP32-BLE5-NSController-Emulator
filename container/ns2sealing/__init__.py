"""The sealing seam (§8.2, §6.4): pure, no device and no HTTP, so key bytes
never cross a request boundary (ADR-0012).

`crypto.py` is the amiitool kernel; `api.py` is the port's tag-image flow —
`unpack` (both HMACs), `pack` (per placement) and `Sealer` (§6.4's lazy
plaintext cache).
"""

from __future__ import annotations

from .api import (
    BCC0_MANUFACTURER_XOR,
    CIPHERTEXT_LENGTH,
    CIPHERTEXT_OFFSET,
    IDENTITY_BLOCK_LENGTH,
    IDENTITY_LENGTH,
    INTERNAL_DATA_HMAC_OFFSET,
    INTERNAL_IDENTITY_OFFSET,
    INTERNAL_LENGTH,
    INTERNAL_TAG_HMAC_OFFSET,
    KEY_LENGTH,
    TAG_DATA_HMAC_OFFSET,
    TAG_IDENTITY_OFFSET,
    TAG_IMAGE_LENGTH,
    TAG_TAG_HMAC_OFFSET,
    TRAILER_LENGTH,
    KeyInvalid,
    KeyMaterial,
    Plaintext,
    SealedTag,
    Sealer,
    TagInvalid,
    bcc0_for,
    bcc1_for,
    identity_block,
    identity_block_for,
    identity_of,
    mint_identity,
    pack,
    seal,
    slice_tag_image,
    unpack,
)
from .crypto import AmiiboKeys, MasterKey, MasterKeyError

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
    "AmiiboKeys",
    "KeyInvalid",
    "KeyMaterial",
    "MasterKey",
    "MasterKeyError",
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
