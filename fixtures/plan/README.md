# The plan golden fixture (§5.7, G-8)

`correction.json` → `correction.plan.hex` → `correction.sha256`.

The fixture is what catches container/firmware drift without a board (§5.7, §12.1
stage 0 item 2). Both sides assert it:

- **Python** — `container/tests/test_golden_fixture.py` compiles the macro and demands
  these exact bytes and hash back, and reproduces §5.7's four-macro acceptance table.
- **C (host)** — `test/host/test_plan_fixture.c` parses the checked-in hex through
  `main/include/protocol/plan.h` and checks the header fields, the frame geometry, the
  magic bytes and the full SHA-256. The header's `static_assert`s are G-13.

The frozen facts — the header bytes, the first plan frame, the SHA-256 and the
four-macro acceptance table — are §5.7's and live there. This directory holds only the
fixture.

## Provenance

`correction.json` is a byte-identical copy of `纠错宏.json` from the pinned canonical
reference (`switch-controller-macro/宏/纠错宏.json`,
`Orangeeeeeeeeeeeeeeeeee/switch-controller-macro@202e512206ab955361aa40e8e2a2735cbe142175`,
cited per `docs/references.md`). Its SHA-256 is
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
