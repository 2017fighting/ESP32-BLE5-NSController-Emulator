#!/usr/bin/env python3
"""Tests for `start_container.py`.

Run:  python3 scripts/test_start_container.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only, like `test_find_serial_port.py`, and hermetic: no Docker, no network, no
corpus, no bench host. Everything asserted here is the *decision* half of the script — which host
paths the run needs, whether they exist, which compose files are used, and what the one generated
override says. The run half is three `subprocess` calls and a health poll.

Two properties are load-bearing, because #42 and #43 were both "the deployment line and the tests
named different things":

- **The mounts are the compose file's, not a copy.** The preflight reads
  `container/compose.yaml`'s own `volumes:` sources and resolves them with the same substitution
  Compose applies, so a fourth mount is covered the day it is added and the wrapper cannot go
  blind by being complete-when-written. `test_the_committed_compose_file_parses_...` fails if the
  file stops being readable this way, and `test_a_compose_file_with_no_volumes_...` is the
  non-vacuous half: "found nothing" must be an error, not a clean bill of health.
- **The generated override touches `devices:` and nothing else.** The image and the mounts are
  `container/compose.yaml`'s own substitutions now (`NS2_IMAGE`, `REFERENCE_ROOT`), so a run that
  re-stated either would be the #42/#43 workaround growing back.
"""

import io
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import start_container as sc  # noqa: E402

#: A compose file shaped like the committed one at the keys the parser reads: the `volumes:` block,
#: an entry whose substitution carries its own `:-`, and a following key that ends the block. Not a
#: copy of the file — the file itself is read by
#: `test_the_committed_compose_file_parses_and_resolves_under_the_root`.
COMPOSE_FIXTURE = """\
services:
  controller:
    image: ${NS2_IMAGE:-ghcr.io/2017fighting/ns2-controller:latest}
    volumes:
      - "${REFERENCE_ROOT:-$HOME/clone}/switch-controller-macro/宏:/library/macros:ro"
      - "${REFERENCE_ROOT:-$HOME/clone}/Amiibo:/library/amiibo:ro"
      - "${REFERENCE_ROOT:-$HOME/clone}/Amiibo/Amiibo Bin/!Essential Files/key_retail.bin:/keys/key_retail.bin:ro"
    ports:
      - "8080:8080"
"""


def _write(path: Path, data: bytes = b"x" * 160) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class MountSourceTest(unittest.TestCase):
    """The preflight's input is the compose file's own list, not a second copy of it."""

    def test_every_volume_source_is_read_in_order(self):
        self.assertEqual(
            sc.mount_sources(COMPOSE_FIXTURE),
            [
                "${REFERENCE_ROOT:-$HOME/clone}/switch-controller-macro/宏",
                "${REFERENCE_ROOT:-$HOME/clone}/Amiibo",
                "${REFERENCE_ROOT:-$HOME/clone}/Amiibo/Amiibo Bin/!Essential Files/key_retail.bin",
            ],
        )

    def test_the_committed_compose_file_parses_and_resolves_under_the_root(self):
        sources = sc.mount_sources(sc.BASE_COMPOSE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(sources), 3, "the committed file's mounts stopped being readable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in sources:
                with self.subTest(source=source):
                    path = sc.mount_source_path(source, root)
                    self.assertTrue(str(path).startswith(str(root)), f"{source!r} escaped the corpus root")
                    self.assertNotIn("${", str(path))

    def test_a_source_outside_the_substitution_is_still_absolute_and_checked(self):
        # A future mount written as a plain host path is checked too, not skipped for being unusual.
        self.assertEqual(sc.mount_source_path("/srv/corpus/Amiibo", Path("/corpus")), Path("/srv/corpus/Amiibo"))
        with self.assertRaises(ValueError):
            sc.mount_source_path("${SOMETHING_ELSE:-/tmp}", Path("/corpus"))

    def test_an_unescaped_dollar_in_the_substitution_default_is_not_a_second_variable(self):
        # Docker reads `${REFERENCE_ROOT:-$HOME/clone}` as one variable; so must this.
        path = sc.mount_source_path("${REFERENCE_ROOT:-$HOME/clone}/Amiibo", Path("/corpus"))
        self.assertEqual(path, Path("/corpus/Amiibo"))


class PreflightTest(unittest.TestCase):
    """`main` must stop before `up`, because Docker turns a missing source into a directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_missing_source_is_named(self):
        (self.root / "Amiibo").mkdir()
        sources = ["${REFERENCE_ROOT:-$HOME/clone}/Amiibo", "${REFERENCE_ROOT:-$HOME/clone}/nope"]
        missing = sc.missing_sources(sources, self.root)
        self.assertEqual(missing, [self.root / "nope"])

    def test_a_present_source_is_not_reported(self):
        (self.root / "Amiibo").mkdir()
        self.assertEqual(sc.missing_sources(["${REFERENCE_ROOT:-$HOME/clone}/Amiibo"], self.root), [])

    def test_the_key_pair_spelling_is_offered_when_only_that_exists(self):
        # §6.7 accepts `unfixed-info.bin` + `locked-secret.bin`; a corpus carrying only that pair
        # would otherwise fail here with a message that reads as "you have no key".
        directory = self.root / "Amiibo" / "Amiibo Bin" / sc.ESSENTIAL_DIR
        for name in sc.TWO_FILE_KEY:
            _write(directory / name, b"y" * 80)
        missing = self.root / "Amiibo" / "Amiibo Bin" / sc.ESSENTIAL_DIR / sc.KEY_FILE
        hint = sc.hint_for(missing)
        self.assertIsNotNone(hint)
        self.assertIn("unfixed-info.bin", hint)

    def test_no_hint_is_invented_for_an_unrelated_source(self):
        self.assertIsNone(sc.hint_for(self.root / "Amiibo"))

    def test_no_hint_when_the_pair_is_not_there_either(self):
        missing = self.root / "Amiibo" / "Amiibo Bin" / sc.ESSENTIAL_DIR / sc.KEY_FILE
        self.assertIsNone(sc.hint_for(missing))


class DevicesOverrideTest(unittest.TestCase):
    """The one generated file, and the two properties #42/#43 would undo if it grew."""

    def test_no_devices_key_means_no_override_at_all(self):
        self.assertIsNone(sc.plan_run_devices(system="linux", port=Path("/dev/ttyACM0"), no_device=False))
        self.assertIsNone(sc.plan_run_devices(system="darwin", port=Path("/dev/cu.usbmodem1"), no_device=False))

    def test_the_device_absent_clears_the_mapping(self):
        self.assertEqual(sc.plan_run_devices(system="darwin", port=None, no_device=True), [])

    def test_a_by_id_pin_that_points_at_the_kernel_node_needs_no_override(self):
        # §10.5's pin is a symlink; a host where it points at /dev/ttyACM0 is a host the committed
        # base file already describes, and generating a file there would contradict §10.4's
        # "only where the committed files cannot say the right thing".
        with tempfile.TemporaryDirectory() as directory:
            pin = Path(directory) / "usb-1a86_USB_Single_Serial_5C93063985-if00"
            pin.symlink_to(sc.CONTAINER_DEVICE_PATH)
            self.assertIsNone(sc.plan_run_devices(system="linux", port=pin, no_device=False))

    def test_a_by_id_node_replaces_the_kernel_path(self):
        with tempfile.TemporaryDirectory() as directory:
            pin = Path(directory) / "usb-1a86_USB_Single_Serial_5C93063985-if00"
            pin.symlink_to("/dev/ttyUSB7")
            self.assertEqual(
                sc.plan_run_devices(system="linux", port=pin, no_device=False),
                [f"{pin}:{sc.CONTAINER_DEVICE_PATH}"],
            )

    def test_the_override_touches_devices_and_nothing_else(self):
        text = sc.render_override([])
        keys = [line.strip().split(":")[0] for line in text.splitlines() if line.startswith("    ") and ":" in line]
        self.assertEqual(keys, ["devices"])
        # `!override` and not a merge: the base file's `/dev/ttyACM0:/dev/ttyACM0` must not
        # survive on a macOS host, where that node does not exist.
        self.assertIn("!override", text)

    def test_neither_the_image_nor_the_mounts_are_restated(self):
        for devices in ([], ["/dev/x:/dev/ttyACM0"]):
            with self.subTest(devices=devices):
                text = sc.render_override(devices)
                self.assertNotIn("image:", text)
                self.assertNotIn("volumes:", text)

    def test_a_path_that_needs_escaping_is_refused_rather_than_mangled(self):
        with self.assertRaises(ValueError):
            sc.render_override(['/dev/we"ird:/dev/ttyACM0'])
        with self.assertRaises(ValueError):
            sc.render_override(["C:\\dev\\com1:/dev/ttyACM0"])


class PlanRunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("/corpus")

    def _plan(self, **overrides):
        arguments = {
            "system": "linux",
            "root": self.root,
            "sources": ["${REFERENCE_ROOT:-$HOME/clone}/Amiibo"],
            "port": Path("/dev/ttyACM0"),
            "no_device": False,
            "image": None,
            "env": {},
        }
        arguments.update(overrides)
        return sc.plan_run(**arguments)

    def test_the_corpus_root_is_exported_once_for_the_files_substitutions(self):
        launch = self._plan()
        self.assertEqual(launch.env["REFERENCE_ROOT"], "/corpus")
        self.assertNotIn("NS2_IMAGE", launch.env)

    def test_a_bench_image_tag_is_a_variable_not_a_new_override(self):
        self.assertEqual(self._plan(image="ns2-controller:dev").env["NS2_IMAGE"], "ns2-controller:dev")

    def test_macos_adds_the_committed_override_and_the_variable_it_reads(self):
        launch = self._plan(system="darwin", port=Path("/dev/cu.usbmodem5C930639851"))
        self.assertEqual(launch.files, [sc.BASE_COMPOSE, sc.MACOS_COMPOSE])
        # `${NS2_PORT:?…}` fails the committed override when it is unset, so the wrapper sets it.
        self.assertEqual(launch.env["NS2_PORT"], "/dev/cu.usbmodem5C930639851")
        self.assertIsNone(launch.override_text)

    def test_macos_without_the_device_drops_that_override_and_clears_devices(self):
        launch = self._plan(system="darwin", port=None, no_device=True)
        self.assertEqual(launch.files, [sc.BASE_COMPOSE])
        self.assertNotIn("NS2_PORT", launch.env)
        self.assertEqual(launch.devices, [])
        self.assertIsNotNone(launch.override_text)

    def test_the_kernel_path_needs_no_generated_file(self):
        self.assertIsNone(self._plan().override_text)

    def test_the_first_file_decides_the_project_name(self):
        self.assertEqual(self._plan().files[0], sc.BASE_COMPOSE)
        self.assertEqual(self._plan().command("up", "-d")[:3], ["docker", "compose", "-f"])

    def test_the_override_comes_last_so_it_cannot_rename_the_project(self):
        launch = self._plan(system="darwin", port=None, no_device=True)
        command = launch.command("up", "-d", override_path="/tmp/o.yaml")
        self.assertEqual(command[-4:], ["-f", "/tmp/o.yaml", "up", "-d"])


class ResolvePortTest(unittest.TestCase):
    def test_an_explicit_node_that_does_not_exist_is_an_error_not_a_guess(self):
        node, why = sc.resolve_port(system="linux", explicit="/dev/nope", env={})
        self.assertIsNone(node)
        self.assertIn("/dev/nope", why)

    def test_macos_honours_ns2_port_and_rejects_a_stale_one(self):
        with tempfile.TemporaryDirectory() as directory:
            node = Path(directory) / "cu.usbmodem5C930639851"
            node.write_text("")
            self.assertEqual(sc.resolve_port(system="darwin", env={"NS2_PORT": str(node)}), (node, ""))
        missing, why = sc.resolve_port(system="darwin", env={"NS2_PORT": "/dev/cu.usbmodem999"})
        self.assertIsNone(missing)
        self.assertIn("NS2_PORT", why)


class ReportTest(unittest.TestCase):
    def test_the_report_carries_the_four_link_facts_and_the_library_counts(self):
        lines = sc.report(
            {
                "control": {"link": "UP", "port": "/dev/ttyACM0", "baud": 115200},
                "firmware": {
                    "fwVersion": "0.1.0",
                    "protoVer": 1,
                    "bootId": "89a58da0",
                    "features": {"macro": True, "amiibo": True, "config": False},
                },
                "console": {"link": "ADVERTISING", "bonded": True},
                "mode": "IDLE",
                "key": "KEY_OK",
                "macros": [{"status": "ready"}, {"status": "rejected"}],
                "figures": [{"id": "a"}, {"id": "b"}],
            }
        )
        self.assertIn("control   UP  /dev/ttyACM0 @115200", lines[0])
        self.assertIn("0.1.0", " ".join(lines))
        self.assertIn("features macro|amiibo", " ".join(lines))
        self.assertIn("console   ADVERTISING (bonded)", lines[2])
        self.assertIn("1 macro(s) ready · 2 figure(s) · key KEY_OK", lines[4])
        self.assertTrue(lines[5].endswith(f":{sc.HTTP_PORT}"))

    def test_a_state_that_is_not_there_yet_does_not_raise(self):
        self.assertIn("?", " ".join(sc.report({})))


class WaitForStateTest(unittest.TestCase):
    """The first HTTP 200 is not readiness: the container serves while the link is down."""

    def test_a_serving_container_with_the_link_down_is_not_ready(self):
        states = [{"control": {"link": "DOWN"}}, {"control": {"link": "UP"}}]
        with unittest.mock.patch.object(sc, "fetch_state", side_effect=states):
            state, ready = sc.wait_for_state(5.0, expect_link=True)
        self.assertTrue(ready)
        self.assertEqual(sc.control_link(state), "UP")

    def test_the_deadline_returns_the_last_state_and_not_ready(self):
        with unittest.mock.patch.object(sc, "fetch_state", return_value={"control": {"link": "DOWN"}}):
            state, ready = sc.wait_for_state(0.0, expect_link=True)
        self.assertFalse(ready)
        self.assertEqual(sc.control_link(state), "DOWN")

    def test_without_a_device_expected_the_first_answer_is_the_result(self):
        # `--no-device` is a documented way to run, so a down link there is success, not exit 4.
        with unittest.mock.patch.object(sc, "fetch_state", return_value={"control": {"link": "DOWN"}}):
            state, ready = sc.wait_for_state(0.0, expect_link=False)
        self.assertTrue(ready)
        self.assertEqual(sc.control_link(state), "DOWN")

    def test_nothing_answering_at_all_is_not_ready_and_has_no_state(self):
        with unittest.mock.patch.object(sc, "fetch_state", return_value=None):
            state, ready = sc.wait_for_state(0.0, expect_link=False)
        self.assertIsNone(state)
        self.assertFalse(ready)


class MainTest(unittest.TestCase):
    def test_a_missing_mount_source_stops_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "switch-controller-macro").mkdir()
            # The macro library is there, the amiibo corpus is not: the run must stop, because
            # Docker would create `Amiibo/` and `Amiibo Bin/!Essential Files/` as directories and
            # the container would report KEY_ABSENT for a key that was never the problem.
            with unittest.mock.patch("subprocess.run") as run:
                stderr = io.StringIO()
                with unittest.mock.patch("sys.stderr", stderr):
                    code = sc.main(["--reference-root", str(root), "--no-build"])
                self.assertEqual(code, 2)
                self.assertIn("missing mount source", stderr.getvalue())
                run.assert_not_called()

    def test_dry_run_prints_the_plan_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in sc.mount_sources(sc.BASE_COMPOSE.read_text(encoding="utf-8")):
                sc.mount_source_path(source, root).mkdir(parents=True, exist_ok=True)
            stdout = io.StringIO()
            with unittest.mock.patch("subprocess.run") as run:
                with redirect_stdout(stdout):
                    code = sc.main(
                        ["--reference-root", str(root), "--no-device", "--dry-run", "--image", "x:dev"]
                    )
            self.assertEqual(code, 0)
            printed = stdout.getvalue()
            self.assertIn("reference root", printed)
            self.assertIn("x:dev", printed)
            self.assertIn("!override []", printed)  # the one generated file, shown before it exists
            self.assertIn("up -d", printed)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
