#!/usr/bin/env python3
"""The golden fixture (§5.7, G-8) and the four-macro acceptance table.

The fixture test is hermetic: it compiles the checked-in ``correction.json``
and demands the checked-in ``correction.plan.hex`` and ``correction.sha256``
back, byte for byte. The acceptance table additionally needs the pinned
reference library (``$REFERENCE_ROOT/switch-controller-macro``); it skips when
that clone is absent, and CI materialises it so the table always runs there.

Run:  python3 -m unittest discover -s container/tests -p 'test_*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2plan import compile_json  # noqa: E402

# The worked example from §5.7, verbatim.
GOLDEN_HEADER = "4e53504c010b47005d660000"
GOLDEN_FRAME_0 = "4000000008800008807d00"
GOLDEN_SHA256 = "1f0a90d3ccabcb198cd236afc4c7572658505bd4ecc429974aff15354661fdfa"

# §5.7's "the real library, compiled" — the acceptance range, not a prediction.
# (reference filename, records, loop_ms, plan bytes, the SHA-256[:16] *printed* in §5.7)
#
# Note: §5.7's column shows 16 hex characters (8 bytes) for readability; §5.6 and
# ADR-0010 make the identity 16 *bytes* (32 hex characters). The table below is
# reproduced as printed, and `test_truncated_identity_is_the_fixture_prefix`
# pins the 16-byte identity separately.
ACCEPTANCE_TABLE = (
    ("天妇罗巢穴宏1.json", 243, 115321, 2685, "9c3aabab06ca2d9f"),
    ("天妇罗巢穴风扇-感谢群友分享.json", 243, 115321, 2685, "140336da0735aac2"),
    ("杏仁巢穴宏.json", 304, 62148, 3356, "d2717773e32486c9"),
    ("纠错宏.json", 71, 26205, 793, "1f0a90d3ccabcb19"),
)


def assert_matches_fixture(test: unittest.TestCase, payload_hex: str) -> None:
    """The one assertion both the fixture and the flip test use."""
    test.assertEqual(payload_hex, support.correction_plan_hex())
    test.assertEqual(
        support.sha256_hex(bytes.fromhex(payload_hex)), support.correction_sha256()
    )


class GoldenFixture(unittest.TestCase):
    def test_header_and_first_frame_are_the_spec_bytes(self):
        payload_hex = support.correction_plan_hex()
        self.assertTrue(payload_hex.startswith(GOLDEN_HEADER + GOLDEN_FRAME_0))

    def test_sha256_is_the_spec_hash(self):
        self.assertEqual(support.correction_sha256(), GOLDEN_SHA256)
        self.assertEqual(
            support.sha256_hex(support.correction_plan_bytes()), GOLDEN_SHA256
        )

    def test_compiler_reproduces_the_fixture_exactly(self):
        plan = compile_json(support.correction_macro_path().read_bytes())
        self.assertEqual((plan.record_count, plan.loop_ms, plan.size), (71, 26205, 793))
        assert_matches_fixture(self, plan.payload.hex())

    def test_truncated_identity_is_the_fixture_prefix(self):
        plan = compile_json(support.correction_macro_path().read_bytes())
        self.assertEqual(plan.identity_hex, GOLDEN_SHA256[:32])

    def test_a_flipped_byte_fails_the_assertion(self):
        """The fixture guard is not vacuous: one flipped byte fails it."""
        original = support.correction_plan_bytes()
        assert_matches_fixture(self, original.hex())  # sanity: it passes unflipped

        for offset in (0, 12, 50, len(original) - 1):
            flipped = bytearray(original)
            flipped[offset] ^= 0x01
            with self.assertRaises(AssertionError, msg=f"offset {offset}"):
                assert_matches_fixture(self, bytes(flipped).hex())


@unittest.skipUnless(
    support.reference_macro_dir() is not None,
    "reference macro library not present ($REFERENCE_ROOT/switch-controller-macro)",
)
class AcceptanceTable(unittest.TestCase):
    def _compile(self, filename: str):
        macro = support.reference_macro_dir() / filename
        return compile_json(macro.read_bytes())

    def test_four_macro_table_reproduces_exactly(self):
        for filename, records, loop_ms, size, identity16 in ACCEPTANCE_TABLE:
            with self.subTest(macro=filename):
                plan = self._compile(filename)
                self.assertEqual(plan.record_count, records)
                self.assertEqual(plan.loop_ms, loop_ms)
                self.assertEqual(plan.size, size)
                self.assertEqual(plan.identity_hex[:16], identity16)

    def test_sum_of_holds_equals_loop_ms(self):
        from container.ns2plan import iter_frames

        for filename, *_ in ACCEPTANCE_TABLE:
            with self.subTest(macro=filename):
                plan = self._compile(filename)
                self.assertEqual(
                    sum(hold for _, hold in iter_frames(plan)), plan.loop_ms
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
