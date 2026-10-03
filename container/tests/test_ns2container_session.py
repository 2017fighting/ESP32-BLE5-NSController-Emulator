#!/usr/bin/env python3
"""The Controller over the *real* session and the byte-level fake board (#30).

`test_ns2container_state.py` exercises the state model against `StubDevice`;
this file exercises the same public surface with `SessionDevice` over
`FrameTransport`, so the load-plan/start/stop path, the §5.6 hash refusal, the
`boot_id` recovery and the `Port busy` refusal are asserted against real frames.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2container_session*.py'
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402
from fake_board import FakeBoard, pipe  # noqa: E402

from container.ns2container import ControllerError, Settings, create_controller  # noqa: E402
from container.ns2device import EventKind, SessionDevice, StubDevice  # noqa: E402
from container.ns2sealing import KeyMaterial, SealedTag, identity_of  # noqa: E402
from container.ns2serial import FrameTransport, PortBusy  # noqa: E402


def fake_sealer(image: bytes, key: KeyMaterial) -> SealedTag:
    return SealedTag(image=image, identity=identity_of(image))


class BusyDevice(StubDevice):
    """A device whose port is held by another process (§8.9)."""

    def __init__(self, holder: str | None = "pid 4242") -> None:
        super().__init__()
        self.holder = holder
        self.hello_calls = 0

    async def hello(self):
        self.hello_calls += 1
        raise PortBusy("/dev/ttyACM0", self.holder, "Resource busy")


class ControllerSessionTest(unittest.IsolatedAsyncioTestCase):
    def build_settings(self) -> Settings:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "macros").mkdir()
        (root / "amiibo").mkdir()
        shutil.copy(support.correction_macro_path(), root / "macros" / "correction.json")
        return Settings(
            macro_dir=root / "macros",
            amiibo_dir=root / "amiibo",
            key_file=root / "keys" / "key_retail.bin",
            key_dir=root / "keys",
            static_dir=root / "dist",
        )

    async def start_with_board(self, **board_kwargs):
        host, device = pipe()
        board = FakeBoard(**board_kwargs)
        board.transport = device
        board.start()
        self.board = board
        self.io = FrameTransport(host, request_timeout=0.5, bulk_timeout=0.25)
        self.controller = create_controller(
            self.build_settings(), device=SessionDevice(self.io), sealer=fake_sealer
        )
        await self.controller.start()
        return self.controller, board

    async def asyncTearDown(self):
        if getattr(self, "controller", None) is not None:
            await self.controller.stop()
        if getattr(self, "board", None) is not None:
            await self.board.stop()
        if getattr(self, "tmp", None) is not None:
            self.tmp.cleanup()

    async def wait_for(self, predicate, *, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    async def test_the_container_drives_the_real_verbs(self):
        controller, board = await self.start_with_board()
        self.assertEqual(controller.snapshot()["control"]["link"], "UP")
        await controller.start_macro("correction.json")
        self.assertEqual(controller.snapshot()["mode"], "MACRO")
        self.assertEqual(board.plan, support.correction_plan_bytes())
        await controller.stop_macro()
        self.assertEqual(controller.snapshot()["mode"], "IDLE")
        self.assertEqual(controller.snapshot()["stopReason"], "CONTAINER_STOP")

    async def test_a_mismatched_echoed_hash_is_refused_not_run(self):
        controller, board = await self.start_with_board()
        board.hash_override = b"\x00" * 16
        with self.assertRaises(ControllerError) as caught:
            await controller.start_macro("correction.json")
        self.assertEqual(caught.exception.code, "PLAN_HASH_MISMATCH")
        self.assertEqual(controller.snapshot()["mode"], "IDLE")

    async def test_a_power_cycle_recovers_without_a_manual_step(self):
        controller, board = await self.start_with_board()
        await controller.start_macro("correction.json")
        self.assertEqual(controller.snapshot()["mode"], "MACRO")

        board.reboot(boot_id=0x99887766)
        self.assertTrue(
            await self.wait_for(lambda: controller.snapshot()["recovery"] == "NEW_POWER")
        )
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertIsNone(snapshot["plan"])
        self.assertEqual(snapshot["control"]["link"], "UP")

    async def test_a_same_boot_id_boot_keeps_the_plan(self):
        # §2.8: BOOT is a hint; whether state survives is the boot_id branch's call.
        controller, board = await self.start_with_board()
        await controller.start_macro("correction.json")
        self.assertEqual(controller.snapshot()["mode"], "MACRO")
        board.emit(EventKind.BOOT, struct.pack("<I", board.hello.boot_id))
        self.assertTrue(
            await self.wait_for(lambda: controller.snapshot()["recovery"] == "SAME_POWER")
        )
        snapshot = controller.snapshot()
        self.assertIsNotNone(snapshot["plan"])
        self.assertEqual(snapshot["mode"], "MACRO")

    async def test_a_held_port_is_surfaced_and_not_retried_through(self):
        busy = BusyDevice()
        self.controller = create_controller(
            self.build_settings(), device=busy, sealer=fake_sealer
        )
        await self.controller.start()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["control"]["link"], "DOWN")
        self.assertEqual(snapshot["control"]["heldBy"], "pid 4242")
        # The connect loop must not keep opening a port it cannot have (§8.9).
        await asyncio.sleep(1.2)
        self.assertEqual(busy.hello_calls, 1)

    async def test_a_verb_on_a_dead_link_is_a_typed_refusal(self):
        controller, board = await self.start_with_board()
        await board.transport.close()
        with self.assertRaises(ControllerError) as caught:
            await controller.start_macro("correction.json")
        self.assertEqual(caught.exception.code, "NO_DEVICE")


if __name__ == "__main__":
    unittest.main()
