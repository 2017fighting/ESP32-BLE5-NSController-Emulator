#!/usr/bin/env python3
"""The sealing seam's facts: the intake slice, the identity coordinates, purity.

The round trip itself is issue #32's; these tests pin the byte coordinates
§6.3/§6.2 fixed and the fact that a placement cannot silently replay a stored
dump — the one failure mode that looks like success.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2sealing*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from container.ns2sealing import (  # noqa: E402
    TAG_IMAGE_LENGTH,
    KeyInvalid,
    KeyMaterial,
    SealingUnavailable,
    bcc0_for,
    bcc1_for,
    identity_block_for,
    identity_of,
    mint_identity,
    seal,
    slice_tag_image,
)

SAMUS_IMAGE = bytes.fromhex("0411fe63ca526c81") + bytes(TAG_IMAGE_LENGTH - 8)


class Intake(unittest.TestCase):
    def test_slice_never_trusts_the_file_size(self):
        self.assertEqual(len(slice_tag_image(bytes(572))), TAG_IMAGE_LENGTH)
        self.assertEqual(len(slice_tag_image(bytes(541))), TAG_IMAGE_LENGTH)

    def test_a_short_file_is_refused(self):
        with self.assertRaises(ValueError):
            slice_tag_image(bytes(539))


class Identity(unittest.TestCase):
    def test_the_seven_byte_uid_skips_bcc0(self):
        # §6.3's worked example: 04 11 fe 63 ca 52 6c 81 -> UID 04 11 fe 63 ca 52 6c.
        self.assertEqual(identity_of(SAMUS_IMAGE).hex(), "0411feca526c81")

    def test_the_check_bytes_are_recomputed(self):
        identity = identity_of(SAMUS_IMAGE)
        self.assertEqual(bcc0_for(identity), 0x63)
        self.assertEqual(bcc1_for(identity), 0x75)
        self.assertEqual(identity_block_for(identity), SAMUS_IMAGE[0:8])

    def test_a_minted_identity_is_nxp_and_seven_bytes(self):
        identity = mint_identity()
        self.assertEqual(len(identity), 7)
        self.assertEqual(identity[0], 0x04)
        self.assertNotEqual(mint_identity(), mint_identity())


class KeyAndSeal(unittest.TestCase):
    def test_key_material_is_exactly_160_bytes(self):
        self.assertEqual(len(KeyMaterial(bytes(160)).data), 160)
        with self.assertRaises(KeyInvalid):
            KeyMaterial(bytes(159))

    def test_sealing_is_not_implemented_in_this_build(self):
        with self.assertRaises(SealingUnavailable):
            seal(SAMUS_IMAGE, KeyMaterial(bytes(160)))


if __name__ == "__main__":
    unittest.main()
