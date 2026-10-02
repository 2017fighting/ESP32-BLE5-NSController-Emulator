#!/usr/bin/env python3
"""Tests for `find_serial_port.py`.

Run:  python3 scripts/test_find_serial_port.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only — this repo has no pytest and no CI, so the test has to run on a bare
host. The fixture in `testdata/ioreg_usb_apple_arm64.txt` is a verbatim excerpt of
`ioreg -p IOUSB -w0 -l` from the arm64 macOS bench host captured while the S3-N16R8 was
attached (2026-10-02); it is kept verbatim on purpose, so the parser is exercised against the
real format — nested one-line dicts, `<hex>` blobs, and device headers that are not USB
devices at all (`Root`, the XHCI controllers).

The ambiguity tests matter more than they look: this host carries a second `usbmodem` node
(a Type-C AV adapter), which is exactly how "the wrong port" (§10.3) happens.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import find_serial_port as fsp  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "testdata" / "ioreg_usb_apple_arm64.txt"


def load_devices():
    return fsp.parse_usb_devices(FIXTURE.read_text())


class ParseIoreg(unittest.TestCase):
    def setUp(self):
        self.devices = load_devices()

    def test_finds_every_device_header_including_non_usb_ones(self):
        names = [device.name for device in self.devices]
        self.assertIn("USB Single Serial", names)
        self.assertIn("USB2.0 Hub", names)
        self.assertIn("USB Type-C Digital AV Adapter", names)
        self.assertIn("Pro2 Controller", names)

    def test_parses_ints_strings_and_location(self):
        board = next(d for d in self.devices if d.name == "USB Single Serial")
        self.assertEqual(board.vendor_id, 0x1A86)
        self.assertEqual(board.product_id, 0x55D3)
        self.assertEqual(board.serial, "5C93063985")
        self.assertEqual(board.product, "USB Single Serial")
        self.assertEqual(board.location_id, 0x1110000)

    def test_nested_one_line_value_is_kept_verbatim_not_misparsed(self):
        board = next(d for d in self.devices if d.name == "USB Single Serial")
        signature = board.properties["UsbDeviceSignature"]
        self.assertTrue(str(signature).startswith("<861ad355"), signature)

    def test_device_without_a_serial_still_parses(self):
        hub = next(d for d in self.devices if d.name == "USB2.0 Hub")
        self.assertEqual(hub.vendor_id, 1507)
        self.assertIsNone(hub.serial)

    def test_non_usb_headers_have_no_vendor_id(self):
        controller = next(d for d in self.devices if d.name.startswith("AppleT8103USBXHCI"))
        self.assertIsNone(controller.vendor_id)


class MatchDevices(unittest.TestCase):
    def test_default_vid_pid_matches_only_the_board(self):
        matched = fsp.match_devices(load_devices())
        self.assertEqual([d.name for d in matched], ["USB Single Serial"])

    def test_serial_narrows_and_a_wrong_serial_matches_nothing(self):
        devices = load_devices()
        self.assertEqual(len(fsp.match_devices(devices, serial="5C93063985")), 1)
        self.assertEqual(fsp.match_devices(devices, serial="DEADBEEF"), [])

    def test_the_av_adapter_is_vid_pid_distinguishable(self):
        # 343c:0000 — a different bridge, and the reason a `usbmodem*` glob is unsafe here.
        matched = fsp.match_devices(load_devices(), vendor_id=0x343C, product_id=0x0000)
        self.assertEqual([d.serial for d in matched], ["000000000000"])


class Resolve(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dev_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, *names):
        for name in names:
            (self.dev_dir / name).touch()

    def test_exactly_one_node(self):
        self.touch("cu.usbmodem5C930639851", "cu.usbmodem0000001")
        nodes = fsp.resolve(load_devices(), dev_dir=self.dev_dir)
        self.assertEqual([n.name for n in nodes], ["cu.usbmodem5C930639851"])

    def test_dialin_node_is_not_returned(self):
        # /dev/tty.* blocks on open until carrier detect; only /dev/cu.* is usable.
        self.touch("tty.usbmodem5C930639851")
        self.assertEqual(fsp.resolve(load_devices(), dev_dir=self.dev_dir), [])

    def test_the_av_adapter_does_not_win_when_the_board_is_absent(self):
        # The trap: a glob would return this and the operator would chase a dead port.
        self.touch("cu.usbmodem0000001")
        self.assertEqual(fsp.resolve(load_devices(), dev_dir=self.dev_dir), [])

    def test_several_interfaces_on_one_device_are_both_reported(self):
        self.touch("cu.usbmodem5C930639851", "cu.usbmodem5C930639852")
        nodes = fsp.resolve(load_devices(), dev_dir=self.dev_dir)
        self.assertEqual(len(nodes), 2)

    def test_no_node_at_all(self):
        self.assertEqual(fsp.resolve(load_devices(), dev_dir=self.dev_dir), [])

    def test_refuses_a_serial_that_could_escape_the_directory(self):
        with self.assertRaises(ValueError):
            fsp.node_for_serial("../../etc/passwd", self.dev_dir)
        with self.assertRaises(ValueError):
            fsp.node_for_serial("", self.dev_dir)


class Cli(unittest.TestCase):
    """The exit codes are the contract a wrapper script depends on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dev_dir = Path(self._tmp.name)
        self.argv = ["--ioreg-file", str(FIXTURE), "--dev-dir", str(self.dev_dir)]

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fsp.main([*self.argv, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_zero_when_exactly_one_node(self):
        (self.dev_dir / "cu.usbmodem5C930639851").touch()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.dev_dir / "cu.usbmodem5C930639851"))

    def test_two_when_nothing_matches(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("no node", err)

    def test_three_when_ambiguous(self):
        (self.dev_dir / "cu.usbmodem5C930639851").touch()
        (self.dev_dir / "cu.usbmodem5C930639852").touch()
        code, _, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("ambiguous", err)

    def test_json_reports_the_bridge_device_even_with_no_node(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 2)
        self.assertIn('"serial": "5C93063985"', out)
        self.assertIn('"nodes": []', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
