#!/usr/bin/env python3
"""Regenerate the checked-in plan fixture from a macro (§5.7).

The fixture is frozen, not derived at test time: this script exists so a
deliberate format change can re-freeze it, and so the bytes are reproducible
from the pinned reference macro. Run it and review the diff; a change to
``*.plan.hex``/``*.sha256`` is a plan-format change and needs the spec amended
(§00's "Amendment mechanics").

    python3 scripts/build_plan_fixture.py fixtures/plan/correction.json \
        fixtures/plan/correction

writes ``<stem>.plan.hex`` and ``<stem>.sha256``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from container.ns2plan import compile_json  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    macro_path = Path(argv[1])
    out_stem = Path(argv[2])
    plan = compile_json(macro_path.read_bytes())
    # `with_suffix` would eat a dot in the stem; append instead.
    Path(str(out_stem) + ".plan.hex").write_text(plan.payload.hex() + "\n")
    # The fixture carries the *full* SHA-256; the 16-byte identity is derived.
    Path(str(out_stem) + ".sha256").write_text(
        hashlib.sha256(plan.payload).hexdigest() + "\n"
    )
    print(
        f"{macro_path.name}: {plan.record_count} records, "
        f"loop_ms={plan.loop_ms}, {plan.size} B, "
        f"identity={plan.identity_hex}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
