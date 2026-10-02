# The plan golden fixture (§5.7, G-8)

`correction.json` → `correction.plan.hex` → `correction.sha256`.

The fixture is what catches container/firmware drift without a board (§5.7, §12.1
stage 0 item 2). Both sides assert it:

- **Python** — `container/tests/test_golden_fixture.py` compiles the macro and demands
  these exact bytes and hash back, and the same file carries §5.7's four-macro
  acceptance table.
- **C (host)** — `test/host/test_plan_fixture.c` parses the checked-in hex through
  `main/include/protocol/plan.h` and checks the header fields, the record geometry, the
  magic bytes and the full SHA-256. Its `static_assert`s are G-13.

## Provenance

`correction.json` is a byte-identical copy of `纠错宏.json` from the pinned canonical
reference `switch-controller-macro` (`docs/references.md`,
`Orangeeeeeeeeeeeeeeeeee/switch-controller-macro@202e512206ab955361aa40e8e2a2735cbe142175`,
path `宏/纠错宏.json`). Its SHA-256 is
`6dff22e4937c99d2b9b90f5417b60caac1771407c2fcdd649add6e16c6158fc6`.

The other three rows of §5.7's table are **not** vendored; the acceptance test reads them
from `$REFERENCE_ROOT/switch-controller-macro/宏` and skips when that clone is absent. CI
clones the pin and runs them.

## Regenerating

```sh
python3 scripts/build_plan_fixture.py fixtures/plan/correction.json fixtures/plan/correction
```

Review the diff. These bytes changing is a plan-format change and needs §5 amended
(§00's "Amendment mechanics"), not a quiet fixture refresh.

## The frozen facts

| | |
| --- | --- |
| events | 88 |
| records | 71 |
| `loop_ms` | 26205 |
| payload | 793 B |
| header | `4e53504c 01 0b 4700 5d660000` |
| frame 0 | `400000 000880 000880 7d00` |
| SHA-256 | `1f0a90d3ccabcb198cd236afc4c7572658505bd4ecc429974aff15354661fdfa` |
| Identity, `SHA-256[:16]` (16 bytes / 32 hex) | `1f0a90d3ccabcb198cd236afc4c75726` |

§5.7's table prints `SHA-256[:16]` as the first **16 hex characters** for readability;
§5.6 and ADR-0010 define the identity as the first **16 bytes**. Both are pinned:
`correction.sha256` carries the full digest, and the identity is its 32-hex prefix.

## §5.7's four-macro acceptance table

| Macro | Events | Records | `loop_ms` | Plan B | `SHA-256[:16]` (as §5.7 prints it) |
| --- | --- | --- | --- | --- | --- |
| 天妇罗巢穴宏1 | 243 | 243 | 115321 | 2685 | `9c3aabab06ca2d9f` |
| 天妇罗巢穴风扇 | 243 | 243 | 115321 | 2685 | `140336da0735aac2` |
| 杏仁巢穴宏 | 411 | 304 | 62148 | 3356 | `d2717773e32486c9` |
| 纠错宏 | 88 | 71 | 26205 | 793 | `1f0a90d3ccabcb19` |
