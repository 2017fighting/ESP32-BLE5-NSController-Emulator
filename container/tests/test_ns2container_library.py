#!/usr/bin/env python3
"""The two library mounts and the key mount (§8.4–§8.6, §6.2, §6.7).

What is pinned: a rejected macro stays listed with its reason, `Rescan` re-reads
the mount, the amiibo index slices 572-byte files and excludes `!Essential
Files/`, and a present key is `KEY_UNVERIFIED` — never a false `KEY_READY`
until issue #32 can run the unpack round trip.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2container_library*.py'
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2container.library import (  # noqa: E402
    ESSENTIAL_DIR,
    AmiiboIndex,
    KeyState,
    KeyStore,
    MacroLibrary,
)

BAD_MACRO = '[{"t": 0, "ev": {"type": "motion", "x": 1}}]'


class MacroLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(support.correction_macro_path(), self.root / "correction.json")
        (self.root / "bad.json").write_text(BAD_MACRO)
        (self.root / "ignore.png").write_bytes(b"not a macro")
        self.library = MacroLibrary(self.root, capacity_bytes=65536)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_json_files_are_macros(self):
        entries = self.library.scan()
        self.assertEqual(sorted(e.source for e in entries), ["bad.json", "correction.json"])

    def test_a_rejected_macro_stays_listed_with_its_reason(self):
        entries = {e.source: e for e in self.library.scan()}
        bad = entries["bad.json"]
        self.assertEqual(bad.status, "rejected")
        self.assertFalse(bad.runnable)
        self.assertIsNotNone(bad.rejection)
        assert bad.rejection is not None
        self.assertIn("motion", bad.rejection.message)

    def test_a_ready_macro_carries_its_compiled_facts(self):
        entries = {e.source: e for e in self.library.scan()}
        ready = entries["correction.json"]
        self.assertTrue(ready.runnable)
        self.assertEqual(ready.bytes, 793)
        self.assertEqual(ready.loop_ms, 26205)
        self.assertEqual(ready.events, 88)
        assert ready.plan is not None
        self.assertEqual(ready.plan.identity_hex, support.correction_sha256()[:32])

    def test_rescan_picks_up_a_new_file(self):
        self.library.scan()
        shutil.copy(support.correction_macro_path(), self.root / "second.json")
        self.assertEqual(len(self.library.scan()), 3)

    def test_identity_is_stable_across_a_rescan(self):
        first = {e.source: e for e in self.library.scan()}["correction.json"]
        second = {e.source: e for e in self.library.scan()}["correction.json"]
        assert first.plan is not None and second.plan is not None
        self.assertEqual(first.plan.identity, second.plan.identity)

    def test_an_unmounted_directory_is_empty_not_an_error(self):
        library = MacroLibrary(self.root / "nope", capacity_bytes=65536)
        self.assertEqual(library.scan(), [])
        self.assertFalse(library.mounted)


class AmiiboIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        series = self.root / "Zelda Amiibo"
        series.mkdir()
        (series / "Link.bin").write_bytes(bytes(540))
        (series / "Zelda.bin").write_bytes(bytes(572))  # 540 + a non-hash trailer
        essential = self.root / ESSENTIAL_DIR
        essential.mkdir()
        (essential / "key_retail.bin").write_bytes(bytes(160))
        self.index = AmiiboIndex(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_essential_files_is_not_a_figure(self):
        sources = [e.source for e in self.index.scan()]
        self.assertNotIn(f"{ESSENTIAL_DIR}/key_retail.bin", sources)
        self.assertEqual(len(sources), 2)

    def test_a_figure_image_is_sliced_to_540(self):
        self.index.scan()
        entry = next(e for e in self.index.entries if e.name == "Zelda")
        self.assertEqual(len(self.index.read_image(entry.id) or b""), 540)

    def test_series_is_derived_and_deduplicated(self):
        self.index.scan()
        self.assertEqual(self.index.series, ["Zelda Amiibo"])


class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_reports_absent(self):
        store = KeyStore(self.root / "key_retail.bin", self.root)
        self.assertIs(store.read().state, KeyState.KEY_ABSENT)

    def test_a_correctly_sized_key_is_unverified_not_ready(self):
        key_file = self.root / "key_retail.bin"
        key_file.write_bytes(bytes(160))
        status = KeyStore(key_file, self.root).read()
        self.assertIs(status.state, KeyState.KEY_UNVERIFIED)
        self.assertEqual(status.spelling, "single file (key_retail.bin)")

    def test_a_wrong_length_is_invalid(self):
        key_file = self.root / "key_retail.bin"
        key_file.write_bytes(bytes(42))
        self.assertIs(KeyStore(key_file, self.root).read().state, KeyState.KEY_INVALID)

    def test_the_two_file_spelling_is_accepted(self):
        (self.root / "unfixed-info.bin").write_bytes(bytes(80))
        (self.root / "locked-secret.bin").write_bytes(bytes(80))
        status = KeyStore(self.root / "missing.bin", self.root).read()
        self.assertIs(status.state, KeyState.KEY_UNVERIFIED)
        self.assertIn("two files", status.spelling or "")


if __name__ == "__main__":
    unittest.main()
