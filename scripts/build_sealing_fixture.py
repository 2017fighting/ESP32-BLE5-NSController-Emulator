#!/usr/bin/env python3
"""Freeze the sealing fixture from the pinned *C* implementation (§6.4, G-9).

The container's sealing path is a port of `socram8888/amiitool`, and the one
independent oracle for it is that same C code. CI has no Amiibo key — it is the
user's own file and is never distributed (§6.7, ADR-0012) — so this script
builds the fixture **with amiitool**, from a synthetic key and a synthetic
plaintext, and freezes its bytes:

```text
key.hex      160 B   the synthetic masterkeys (unfixed infos + locked secret)
tag.hex      540 B   amiitool -e of the synthetic plaintext: a valid tag image
plain.hex    520 B   amiitool -d of tag.hex: the internal-coordinate cache
trailer.hex   20 B   pages 130-134, the part no internal layout covers
identity.hex   7 B   a fresh `0x04` + 6 bytes identity
sealed.hex   540 B   amiitool -e of plain.hex with that identity written in
```

`plain.hex` and `sealed.hex` are amiitool's own output, not this repo's, so the
Python port is checked against the thing it was ported from rather than against
itself.

The identity is written into `plain.hex` by byte surgery here — three bytes,
`BCC0` at internal `0x1D7` and `BCC1` at internal `0x000` — because amiitool has
no notion of minting a new identity: it seals whatever identity the plaintext
already carries. That is exactly the one step the container adds (§6.3).

Usage:

```sh
git clone https://github.com/socram8888/amiitool /tmp/amiitool
git -C /tmp/amiitool checkout 4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3
git -C /tmp/amiitool submodule update --init --recursive
make -C /tmp/amiitool
python3 scripts/build_sealing_fixture.py --amiitool /tmp/amiitool/amiitool fixtures/sealing
```

Review the diff. These bytes changing means the crypto changed, which needs
§6 amended rather than a quiet fixture refresh.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

# §6.3 / amiitool's `nfc3d_keygen_masterkeys` (`amiitool/include/nfc3d/keygen.h`):
# hmacKey[16] || typeString[14] || rfu || magicBytesSize || magicBytes[16] || xorPad[32].
MASTERKEY_LENGTH = 80
KEY_LENGTH = 160
INTERNAL_LENGTH = 520
TRAILER_LENGTH = 20
TAG_IMAGE_LENGTH = 540
IDENTITY_LENGTH = 7

# amiitool's own names for the two key sets (`amiitool/README.md`: "the key is
# the concatenation of unfixed infos and locked secret keys"). They are format
# labels, not secret material: the synthetic key below is generated here.
DATA_LABEL = b"unfixed infos\x00"
TAG_LABEL = b"locked secret\x00"

BCC0_MANUFACTURER_XOR = 0x88

# The synthetic key's two `magicBytesSize`s mirror the retail file's structure
# (14 for unfixed infos, 16 for locked secret); the bytes themselves do not.
DATA_MAGIC_BYTES_SIZE = 14
TAG_MAGIC_BYTES_SIZE = 16


def stream(seed: bytes, length: int) -> bytes:
    """Deterministic pseudo-random bytes, so the fixture is reproducible."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def masterkey(seed: bytes, label: bytes, magic_bytes_size: int) -> bytes:
    return (
        stream(seed + b"hmac", 16)
        + label
        + b"\x00"  # rfu
        + bytes([magic_bytes_size])
        + stream(seed + b"magic", 16)
        + stream(seed + b"xor", 32)
    )


def synthetic_key() -> bytes:
    key = masterkey(b"ns2-fixture-data", DATA_LABEL, DATA_MAGIC_BYTES_SIZE)
    key += masterkey(b"ns2-fixture-tag", TAG_LABEL, TAG_MAGIC_BYTES_SIZE)
    assert len(key) == KEY_LENGTH
    return key


def identity_block(identity: bytes) -> bytes:
    """`UID[0..2] || BCC0 || UID[3..6]` (§6.3)."""
    bcc0 = BCC0_MANUFACTURER_XOR ^ identity[0] ^ identity[1] ^ identity[2]
    return identity[:3] + bytes([bcc0]) + identity[3:]


def bcc1(identity: bytes) -> int:
    return identity[3] ^ identity[4] ^ identity[5] ^ identity[6]


def synthetic_plaintext() -> tuple[bytes, bytes, bytes]:
    """A synthetic internal-coordinate cache, trailer and the identity it carries."""
    internal = bytearray(stream(b"ns2-fixture-plain", INTERNAL_LENGTH))
    trailer = stream(b"ns2-fixture-trailer", TRAILER_LENGTH)
    identity = bytes([0x04]) + stream(b"ns2-fixture-uid", 6)
    internal[0x1D4:0x1DC] = identity_block(identity)
    internal[0x000] = bcc1(identity)
    return bytes(internal), trailer, identity


def run_amiitool(binary: Path, key: Path, mode: str, payload: bytes, work: Path) -> bytes:
    source = work / "input.bin"
    target = work / "output.bin"
    source.write_bytes(payload)
    subprocess.run(
        [str(binary), mode, "-k", str(key), "-i", str(source), "-o", str(target)],
        check=True,
        capture_output=True,
    )
    return target.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="freeze the sealing fixture with amiitool")
    parser.add_argument("--amiitool", required=True, type=Path, help="the built amiitool binary")
    parser.add_argument("out", type=Path, help="the fixture directory, e.g. fixtures/sealing")
    args = parser.parse_args(argv)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".work"
    work.mkdir(exist_ok=True)

    key = synthetic_key()
    key_file = work / "key_retail.bin"
    key_file.write_bytes(key)

    internal, trailer, identity = synthetic_plaintext()

    # The tag the container will unpack: amiitool seals the synthetic cache.
    tag = run_amiitool(args.amiitool, key_file, "-e", internal + trailer, work)
    if len(tag) != TAG_IMAGE_LENGTH:
        raise SystemExit(f"amiitool -e produced {len(tag)} bytes, expected {TAG_IMAGE_LENGTH}")

    # ... and the same tag unpacked back is the internal cache, HMACs recomputed.
    unpacked = run_amiitool(args.amiitool, key_file, "-d", tag, work)
    plain = unpacked[:INTERNAL_LENGTH]
    if unpacked[INTERNAL_LENGTH:] != trailer:
        raise SystemExit("amiitool -d did not copy the 20-byte trailer verbatim")

    # A fresh identity, written in by hand: the three BCC/UID byte positions.
    fresh = bytes([0x04]) + stream(b"ns2-fixture-fresh-uid", 6)
    rewritten = bytearray(plain)
    rewritten[0x1D4:0x1DC] = identity_block(fresh)
    rewritten[0x000] = bcc1(fresh)
    sealed = run_amiitool(args.amiitool, key_file, "-e", bytes(rewritten) + trailer, work)
    if len(sealed) != TAG_IMAGE_LENGTH:
        raise SystemExit(f"amiitool -e produced {len(sealed)} bytes, expected {TAG_IMAGE_LENGTH}")

    for name, payload in (
        ("key.hex", key),
        ("tag.hex", tag),
        ("plain.hex", plain),
        ("trailer.hex", trailer),
        ("identity.hex", fresh),
        ("sealed.hex", sealed),
    ):
        (out_dir / name).write_text(payload.hex() + "\n")

    # The fixture's own invariant, checked before it is written down: amiitool
    # must both unpack `sealed.hex` and re-seal it to the same bytes. (Unpacking
    # overwrites the two HMAC slots, so the check is the round trip, not the
    # input's stale signatures.)
    check = run_amiitool(args.amiitool, key_file, "-d", sealed, work)
    if check[INTERNAL_LENGTH:] != trailer:
        raise SystemExit("amiitool -d did not copy the trailer of its own sealed tag")
    if run_amiitool(args.amiitool, key_file, "-e", check, work) != sealed:
        raise SystemExit("amiitool disagrees with its own sealed output")

    print(f"wrote {out_dir} from {args.amiitool} (source identity {identity.hex()}, fresh {fresh.hex()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
