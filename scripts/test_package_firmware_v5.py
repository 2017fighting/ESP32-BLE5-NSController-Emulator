#!/usr/bin/env python3
"""Tests for `package_firmware_v5.py`.

Run:  python3 scripts/test_package_firmware_v5.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only — this repo has no pytest and no CI, so the test has to run on a bare
host.

The regression these tests pin down is a real bench trap (§10.1's recipe, hit on the arm64
macOS host on 2026-10-02): the script shelled out to a bare `esptool`, but ESP-IDF v5.5.5's
venv ships **esptool v4.12.0**, whose console script is named `esptool.py`. The rename to
`esptool` lands in esptool v5. So the last step of the documented build recipe failed with
`Error: esptool not found` *after* a successful build — and it was worked around on the host by
symlinking `esptool` -> `esptool.py` inside the IDF venv, which `idf_tools.py
install-python-env` would wipe.

`python -m esptool` is the spelling that works for every esptool the IDF venv has shipped, and
it is the one ESP-IDF's own flash instructions use (`idf.py build` prints
`python -m esptool --chip esp32s3 ... write_flash`). The tests therefore assert the *launcher*,
not a string somewhere in the command.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_firmware_v5 as pkg  # noqa: E402

# Verbatim shape of a real `build/flash_args` (ESP32-S3, 16 MB layout, issue #18's build).
FLASH_ARGS = [
    "--flash_mode dio --flash_freq 80m --flash_size 16MB",
    "0x0 bootloader/bootloader.bin",
    "0x20000 ESP32-BLE5-NSController-Emulator.bin",
    "0x8000 partition_table/partition-table.bin",
    "0xf000 ota_data_initial.bin",
]

OUT = "/tmp/ns-controller-esp32s3n16.bin"


class EsptoolLauncher(unittest.TestCase):
    """The bug: a bare `esptool` is not on PATH in an ESP-IDF v5.5.5 venv."""

    def test_runs_esptool_as_a_module_of_the_running_interpreter(self):
        # Equality, not a substring check: the first element is the interpreter, so no bare
        # `esptool` (which is `esptool.py` in esptool v4 and `esptool` only from v5) can be
        # reached by accident.
        self.assertEqual(pkg.esptool_argv(), [sys.executable, "-m", "esptool"])

    @unittest.skipUnless(
        importlib.util.find_spec("esptool") is not None,
        "esptool not installed in this interpreter; the launcher cannot be exercised",
    )
    def test_the_launcher_is_importable_by_this_interpreter(self):
        # Catches a future esptool renaming the module, which would silently break
        # `python -m esptool` while leaving the string assertions above green.
        import esptool  # noqa: F401


class MergeCommand(unittest.TestCase):
    def setUp(self):
        self.cmd = pkg.build_merge_command("esp32s3", FLASH_ARGS, OUT)

    def test_launches_through_the_module_form(self):
        self.assertEqual(self.cmd[:3], [sys.executable, "-m", "esptool"])

    def test_names_the_chip_and_the_merge_subcommand(self):
        self.assertEqual(self.cmd[3:5], ["--chip", "esp32s3"])
        self.assertEqual(self.cmd[5], "merge_bin")

    def test_passes_the_flash_parameters_through_verbatim(self):
        self.assertEqual(
            self.cmd[6:12],
            ["--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", "16MB"],
        )

    def test_writes_to_the_output_path(self):
        self.assertEqual(self.cmd[12:14], ["-o", OUT])

    def test_keeps_offset_and_file_pairs_in_order(self):
        self.assertEqual(
            self.cmd[14:],
            [
                "0x0", "bootloader/bootloader.bin",
                "0x20000", "ESP32-BLE5-NSController-Emulator.bin",
                "0x8000", "partition_table/partition-table.bin",
                "0xf000", "ota_data_initial.bin",
            ],
        )

    def test_skips_blank_lines_in_the_pairs(self):
        cmd = pkg.build_merge_command(
            "esp32s3", ["--flash_size 8MB", "0x0 a.bin", "", "   ", "0x10000 b.bin"], OUT
        )
        self.assertEqual(cmd[10:], ["0x0", "a.bin", "0x10000", "b.bin"])


class FlashSize(unittest.TestCase):
    """`--flash_size` vs `--flash-size` is the trap that makes the unsuffixed script exit 0."""

    def test_reads_the_underscore_spelling_flash_args_emits(self):
        self.assertEqual(pkg.extract_flash_size_from_args(FLASH_ARGS), 16)

    def test_returns_none_when_no_flash_size_is_present(self):
        self.assertIsNone(pkg.extract_flash_size_from_args(["--flash_mode dio", "0x0 a.bin"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
