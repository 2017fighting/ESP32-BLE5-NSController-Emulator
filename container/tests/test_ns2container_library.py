#!/usr/bin/env python3
"""The two library mounts and the key mount (§8.4–§8.6, §6.2, §6.7).

What is pinned: a rejected macro stays listed with its reason, `Rescan` re-reads
the mount, the amiibo index slices 572-byte files and excludes `!Essential
Files/`, and the key ladder's four states are each reachable — including
`KEY_OK`, which needs a real round trip against a real tag.

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

from container.ns2container import Settings  # noqa: E402
from container.ns2container.library import (  # noqa: E402
    ESSENTIAL_DIR,
    AmiiboIndex,
    KeyState,
    KeyStore,
    MacroLibrary,
)
from container.ns2sealing import KeyMaterial  # noqa: E402

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
        (series / "Link.nfc").write_bytes(b"UID: 04 11 FE 63 CA 52 6C 81\n" * 20)
        other = self.root / "Mario Amiibo"
        other.mkdir()
        (other / "Link.bin").write_bytes(bytes(540))  # same stem, other series
        essential = self.root / ESSENTIAL_DIR
        essential.mkdir()
        (essential / "key_retail.bin").write_bytes(bytes(160))
        self.index = AmiiboIndex(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_essential_files_is_not_a_figure(self):
        sources = [e.source for e in self.index.scan()]
        self.assertNotIn(f"{ESSENTIAL_DIR}/key_retail.bin", sources)
        self.assertEqual(len(sources), 3)

    def test_nfc_wrappers_are_ignored_not_decoded(self):
        sources = [e.source for e in self.index.scan()]
        self.assertFalse([source for source in sources if source.endswith(".nfc")])

    def test_two_figures_with_the_same_name_are_two_files(self):
        # §6.2: the index is keyed by file, never by ID or by stem.
        self.index.scan()
        self.assertEqual(len([e for e in self.index.entries if e.name == "Link"]), 2)
        self.assertEqual(len({e.id for e in self.index.entries}), len(self.index.entries))

    def test_a_figure_image_is_sliced_to_540(self):
        self.index.scan()
        entry = next(e for e in self.index.entries if e.name == "Zelda")
        self.assertEqual(len(self.index.read_image(entry.id) or b""), 540)

    def test_series_is_derived_and_deduplicated(self):
        self.index.scan()
        self.assertEqual(self.index.series, ["Mario Amiibo", "Zelda Amiibo"])

    def test_figures_carry_their_series_so_the_ui_can_group_them(self):
        # §8.5's "grouped by figure and series": the entries carry both, and the
        # Control screen's picker filters on them. There is deliberately no
        # second, server-side grouping a view would have to keep in step.
        self.index.scan()
        self.assertEqual({(e.series, e.name) for e in self.index.entries}, {
            ("Mario Amiibo", "Link"),
            ("Zelda Amiibo", "Link"),
            ("Zelda Amiibo", "Zelda"),
        })

    def test_the_sample_is_bounded_and_deterministic(self):
        self.index.scan()
        first = self.index.sample_images(2)
        self.assertEqual(len(first), 2)
        self.assertEqual(first, self.index.sample_images(2))
        self.assertEqual(len(self.index.sample_images(8)), 3)


class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_file = self.root / "key_retail.bin"
        self.key_file.write_bytes(support.sealing_fixture("key"))
        self.index = AmiiboIndex(self.root / "amiibo")
        (self.root / "amiibo" / "Zelda").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_figures(self, *images: bytes) -> None:
        for number, image in enumerate(images):
            (self.root / "amiibo" / "Zelda" / f"Figure{number}.bin").write_bytes(image)
        self.index.scan()

    def store(self) -> KeyStore:
        return KeyStore(self.key_file, self.root, index=self.index)

    def test_absent_reports_absent(self):
        self.key_file.unlink()
        status = KeyStore(self.root / "key_retail.bin", self.root).read()
        self.assertIs(status.state, KeyState.KEY_ABSENT)
        self.assertIsNone(status.fingerprint)

    def test_a_key_with_no_library_tag_is_unverified(self):
        # §6.7: held but unproven is the honest answer, not "valid" or "invalid".
        self.write_figures()
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_UNVERIFIED)
        self.assertEqual(status.spelling, "single file")
        self.assertIsNotNone(status.material)

    def test_a_key_that_verifies_a_library_figure_is_ok(self):
        self.write_figures(support.sealing_fixture("tag"))
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_OK)
        self.assertEqual(len(status.fingerprint or ""), 16)
        self.assertEqual(status.length, 160)

    def test_a_bad_dump_does_not_condemn_the_key(self):
        # The corpus ships one genuinely bad dump (Pikmin, amiitool agrees), so
        # rung 3 samples several figures: a good key is not falsely accused.
        corrupted = bytearray(support.sealing_fixture("tag"))
        corrupted[0x080] ^= 0x01
        self.write_figures(bytes(corrupted), support.sealing_fixture("tag"))
        self.assertIs(self.store().read().state, KeyState.KEY_OK)

    def test_a_key_that_verifies_nothing_is_invalid(self):
        corrupted = bytearray(support.sealing_fixture("tag"))
        corrupted[0x080] ^= 0x01
        self.write_figures(bytes(corrupted))
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_INVALID)
        self.assertIn("verified", status.detail or "")

    def test_a_rejected_key_still_carries_its_fingerprint(self):
        # §6.7: without it, two different wrong keys are indistinguishable in a log.
        self.key_file.write_bytes(bytes(160))
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_INVALID)
        self.assertEqual(len(status.fingerprint or ""), 16)

    def test_two_different_wrong_keys_fingerprint_differently(self):
        first = bytearray(support.sealing_fixture("key"))
        first[0] ^= 0x01
        self.key_file.write_bytes(bytes(first))
        self.write_figures(support.sealing_fixture("tag"))
        one = self.store().read()
        second = bytearray(support.sealing_fixture("key"))
        second[0] ^= 0x02
        self.key_file.write_bytes(bytes(second))
        two = self.store().read()
        self.assertIs(one.state, KeyState.KEY_INVALID)
        self.assertIs(two.state, KeyState.KEY_INVALID)
        self.assertNotEqual(one.fingerprint, two.fingerprint)

    def test_a_wrong_length_is_invalid(self):
        self.key_file.write_bytes(bytes(42))
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_INVALID)
        self.assertEqual(status.length, 42)
        # Even a rejected file is identifiable: the length says it is not a key,
        # the fingerprint says *which* not-a-key it is (§6.7).
        self.assertEqual(len(status.fingerprint or ""), 16)

    def test_a_random_160_byte_file_fails_the_structure_rung(self):
        self.key_file.write_bytes(bytes(160))
        self.write_figures(support.sealing_fixture("tag"))
        self.assertIs(self.store().read().state, KeyState.KEY_INVALID)

    def test_the_two_file_spelling_is_accepted(self):
        key = support.sealing_fixture("key")
        (self.root / "unfixed-info.bin").write_bytes(key[:80])
        (self.root / "locked-secret.bin").write_bytes(key[80:])
        self.key_file.unlink()
        status = self.store().read()
        self.assertIs(status.state, KeyState.KEY_UNVERIFIED)
        self.assertIn("two files", status.spelling or "")

    def test_the_key_bytes_are_kept_out_of_repr(self):
        # A key in a traceback or a log line has left the process (§6.7).
        material = KeyMaterial(support.sealing_fixture("key"))
        self.assertNotIn(support.sealing_fixture("key")[:16].hex(), repr(material))
        self.assertNotIn("data", repr(material))


class KeyPathTest(unittest.TestCase):
    """§6.7: the key's path is fixed, so the lock can name it truthfully."""

    def test_the_key_path_has_no_environment_variable(self):
        settings = Settings.from_env(
            {"NS2_KEY_FILE": "/tmp/elsewhere.bin", "NS2_KEY_DIR": "/tmp/elsewhere"}
        )
        self.assertEqual(settings.key_file, Path("/keys/key_retail.bin"))
        self.assertEqual(settings.key_dir, Path("/keys"))

    def test_the_port_and_the_library_mounts_are_still_overridable(self):
        # For a host run with no mounts; the key is the deliberate exception.
        settings = Settings.from_env({"NS2_MACRO_DIR": "/tmp/macros", "CONTROLLER_PORT": "/dev/null"})
        self.assertEqual(settings.macro_dir, Path("/tmp/macros"))
        self.assertEqual(settings.port, "/dev/null")


if __name__ == "__main__":
    unittest.main()
