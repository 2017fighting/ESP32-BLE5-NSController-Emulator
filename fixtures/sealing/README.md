# The sealing golden fixture (§6.4, G-9)

A synthetic key, a synthetic tag and **amiitool's own output bytes** for the round trip the
container ports. The container's sealing path is a port of `socram8888/amiitool`
(`docs/references.md`), so the C build is the only independent oracle for it — and CI has no
Amiibo key to run it with, because the key is the user's own file and is never distributed
(§6.7, ADR-0012).

| File | Bytes | Is |
| --- | --- | --- |
| `key.hex` | 160 | the synthetic masterkeys (`unfixed infos` + `locked secret`) |
| `tag.hex` | 540 | `amiitool -e` of the synthetic plaintext: a valid tag image |
| `plain.hex` | 520 | `amiitool -d` of `tag.hex`: the internal-coordinate plaintext cache |
| `trailer.hex` | 20 | pages 130–134, copied verbatim; no internal layout covers them |
| `identity.hex` | 7 | a fresh `0x04` + 6 bytes identity |
| `sealed.hex` | 540 | `amiitool -e` of `plain.hex` with `identity.hex` written in |

`plain.hex` and `sealed.hex` are amiitool's output, not this repo's, so
`container/tests/test_ns2sealing.py` checks the Python port against the thing it was ported
from rather than against itself. Everything here is synthetic — no retail key material is
vendored, and the fixture is deliberately *not* generated from the container's own code. The
synthetic masterkeys do reuse the format's two `typeString` labels and their `magicBytesSize`
values (`unfixed infos`/`\0`/`0x0e` and `locked secret`/`\0`/`0x10`), because those are structural
constants amiitool names in its own README; every secret field — both `hmacKey`s, both
`magicBytes` and both `xorPad`s — is generated here and shares no byte with a retail key.

The identity is written into `plain.hex` by byte surgery in the generator (three positions:
`BCC0` at internal `0x1D7`, `BCC1` at internal `0x000`) because amiitool seals whatever
identity a plaintext already carries; minting is the one step §6.3 adds container-side.

## Provenance

Built by `scripts/build_sealing_fixture.py` from `socram8888/amiitool` at
`4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3` (the pin in `docs/references.md`), whose
`nfc3d_amiibo_unpack` / `nfc3d_amiibo_pack` and `nfc3d_keygen` define these bytes.

## Regenerating

```sh
git clone https://github.com/socram8888/amiitool /tmp/amiitool
git -C /tmp/amiitool checkout 4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3
git -C /tmp/amiitool submodule update --init --recursive
make -C /tmp/amiitool
python3 scripts/build_sealing_fixture.py --amiitool /tmp/amiitool/amiitool fixtures/sealing
```

Review the diff. These bytes changing is a change to the sealing flow and needs §6 amended
(§00's "Amendment mechanics"), not a quiet fixture refresh.

## The real corpus

The same tests run against the real Amiibo clone when it is mounted:
`$REFERENCE_ROOT/Amiibo/Amiibo Bin` (`.bin` images) and `!Essential Files/key_retail.bin`
(the key). That half skips when the clone or the key is absent, which is the normal CI case.
