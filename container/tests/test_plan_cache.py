#!/usr/bin/env python3
"""The memory-only plan cache and the hash contract (§8.4, §5.6, ADR-0010)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2plan import (  # noqa: E402
    MacroRejected,
    PlanCache,
    PlanHashMismatch,
    assert_echo,
    hash_matches,
)

MACRO_A = b'[{"t":0,"ev":{"type":"button","name":"a","pressed":true}}]'
MACRO_B = b'[{"t":0,"ev":{"type":"button","name":"b","pressed":true}}]'
MACRO_BROKEN = b'[{"t":0,"ev":{"type":"button","name":"turbo","pressed":true}}]'


class PlanCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cache = PlanCache()

    def write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def bump_mtime(self, path: Path) -> None:
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def test_unchanged_file_is_a_cache_hit(self):
        path = self.write("a.json", MACRO_A)
        first = self.cache.load(path)
        second = self.cache.load(path)
        self.assertIs(first, second)
        self.assertEqual(len(self.cache), 1)

    def test_changed_file_recompiles_and_forgets_the_old_revision(self):
        path = self.write("a.json", MACRO_A)
        first = self.cache.load(path)
        path.write_bytes(MACRO_B)
        self.bump_mtime(path)
        second = self.cache.load(path)
        self.assertNotEqual(first.payload, second.payload)
        self.assertEqual(len(self.cache), 1)

    def test_invalidate_drops_entries(self):
        path = self.write("a.json", MACRO_A)
        self.cache.load(path)
        self.cache.invalidate(path)
        self.assertEqual(len(self.cache), 0)
        self.assertNotIn(path, self.cache)

    def test_clear_drops_everything(self):
        self.cache.load(self.write("a.json", MACRO_A))
        self.cache.load(self.write("b.json", MACRO_B))
        self.assertEqual(len(self.cache), 2)
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)

    def test_capacity_precheck_applies_to_cache_hits(self):
        path = self.write("a.json", MACRO_A)
        plan = self.cache.load(path)
        self.assertEqual(plan.size, 23)
        self.cache.load(path, capacity_bytes=23)  # exactly fits
        with self.assertRaises(MacroRejected) as ctx:
            self.cache.load(path, capacity_bytes=22)
        self.assertEqual(ctx.exception.code, "PLAN_TOO_LARGE")

    def test_rejected_macro_is_not_cached(self):
        path = self.write("bad.json", MACRO_BROKEN)
        with self.assertRaises(MacroRejected):
            self.cache.load(path)
        self.assertEqual(len(self.cache), 0)


class HashContractTests(unittest.TestCase):
    def test_fixture_identity_is_the_truncated_hash(self):
        cache = PlanCache()
        plan = cache.load(support.correction_macro_path())
        self.assertEqual(plan.identity_hex, support.correction_sha256()[:32])

    def test_echo_agreement_passes_and_disagreement_raises(self):
        cache = PlanCache()
        plan = cache.load(support.correction_macro_path())
        self.assertTrue(hash_matches(plan.identity, plan.identity))
        assert_echo(plan.identity, plan.identity)
        flipped = bytes([plan.identity[0] ^ 0x01]) + plan.identity[1:]
        self.assertFalse(hash_matches(plan.identity, flipped))
        with self.assertRaises(PlanHashMismatch):
            assert_echo(plan.identity, flipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
