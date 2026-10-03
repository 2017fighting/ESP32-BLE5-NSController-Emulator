#!/usr/bin/env python3
"""The container state model: device truth plus the container's own facts (§8.7).

The screens are only as honest as this projection, so what is pinned here is
the separation §8.6 insists on (three amiibo locks stay distinct), the recovery
cases of §2.8, the no-optimistic-mode rule, and the fact that a lock is refused
container-side without a frame.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2container_state*.py'
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2container import ControllerError, Settings, create_controller  # noqa: E402
from container.ns2device import ConsoleLink, Event, EventKind, Hello, StubDevice  # noqa: E402
from container.ns2sealing import KeyMaterial, SealedTag, identity_of  # noqa: E402

TAG = bytes.fromhex("0411fe63ca526c81") + bytes(532)


def fake_sealer(image: bytes, key: KeyMaterial) -> SealedTag:
    """A test double, not a fallback: it copies the source image's identity."""
    return SealedTag(image=image, identity=identity_of(image))


class SlowStubDevice(StubDevice):
    """A stub whose transfer takes a tick per chunk, so cancellation can interleave."""

    async def load_plan(self, plan_hash, plan, *, on_progress=None):
        total = len(plan)
        offset = 0
        while offset < total:
            await asyncio.sleep(0.01)
            offset = min(total, offset + 256)
            if on_progress is not None:
                on_progress(offset, total)
        await super().load_plan(plan_hash, plan, on_progress=None)


class ControllerTest(unittest.IsolatedAsyncioTestCase):
    def build_settings(self, *, with_key: bool = True) -> Settings:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "macros").mkdir()
        (root / "amiibo" / "Zelda").mkdir(parents=True)
        shutil.copy(support.correction_macro_path(), root / "macros" / "correction.json")
        (root / "macros" / "bad.json").write_text('[{"t": 0, "ev": {"type": "motion"}}]')
        (root / "amiibo" / "Zelda" / "Link.bin").write_bytes(TAG)
        key_file = root / "keys" / "key_retail.bin"
        key_file.parent.mkdir()
        if with_key:
            key_file.write_bytes(bytes(160))
        return Settings(
            macro_dir=root / "macros",
            amiibo_dir=root / "amiibo",
            key_file=key_file,
            key_dir=key_file.parent,
            static_dir=root / "dist",
        )

    async def start_controller(self, *, device=None, with_key: bool = True):
        settings = self.build_settings(with_key=with_key)
        self.controller = create_controller(
            settings,
            device=device if device is not None else StubDevice(),
            sealer=fake_sealer,
        )
        await self.controller.start()
        return self.controller

    async def asyncTearDown(self):
        if getattr(self, "controller", None) is not None:
            await self.controller.stop()
        if getattr(self, "tmp", None) is not None:
            self.tmp.cleanup()

    async def test_startup_snapshot_is_honest(self):
        snapshot = (await self.start_controller()).snapshot()
        self.assertEqual(snapshot["control"]["link"], "UP")
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertEqual(snapshot["firmware"]["features"]["amiibo"], True)
        self.assertEqual(snapshot["macroLibrary"], "READY")
        self.assertEqual(snapshot["amiiboLibrary"], "READY")
        self.assertEqual(snapshot["key"], "KEY_UNVERIFIED")
        self.assertEqual([m["status"] for m in snapshot["macros"]], ["rejected", "ready"])

    async def test_start_then_stop_moves_only_through_the_device(self):
        controller = await self.start_controller()
        await controller.start_macro("correction.json")
        running = controller.snapshot()
        self.assertEqual(running["mode"], "MACRO")
        self.assertEqual(running["plan"]["hash"], support.correction_sha256()[:32])
        self.assertEqual(running["plan"]["bytes"], 793)
        await controller.stop_macro()
        stopped = controller.snapshot()
        self.assertEqual(stopped["mode"], "IDLE")
        self.assertEqual(stopped["stopReason"], "CONTAINER_STOP")
        self.assertIsNotNone(stopped["plan"])

    async def test_an_unknown_macro_is_refused_without_a_frame(self):
        controller = await self.start_controller()
        with self.assertRaises(ControllerError) as caught:
            await controller.start_macro("nope.json")
        self.assertEqual(caught.exception.code, "UNKNOWN_MACRO")

    async def test_a_rejected_macro_is_refused_with_its_reason(self):
        controller = await self.start_controller()
        with self.assertRaises(ControllerError) as caught:
            await controller.start_macro("bad.json")
        self.assertEqual(caught.exception.code, "MACRO_REJECTED")
        self.assertIn("motion", caught.exception.message)

    async def test_placing_with_no_key_is_refused_container_side(self):
        controller = await self.start_controller(with_key=False)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["key"], "KEY_ABSENT")
        self.assertEqual(snapshot["amiiboLibrary"], "READY")
        with self.assertRaises(ControllerError) as caught:
            await controller.place_figure("Zelda/Link.bin")
        self.assertEqual(caught.exception.code, "KEY_ABSENT")
        self.assertEqual(controller.snapshot()["mode"], "IDLE")

    async def test_placing_with_a_key_places_a_tag(self):
        controller = await self.start_controller()
        await controller.place_figure("Zelda/Link.bin")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "AMIIBO")
        self.assertEqual(snapshot["placement"]["figureId"], "Zelda/Link.bin")
        self.assertEqual(snapshot["placement"]["identity"], "0411feca526c81")
        self.assertEqual(snapshot["placement"]["index"], 1)
        await controller.unplace()
        self.assertEqual(controller.snapshot()["mode"], "IDLE")

    async def test_placement_while_macro_is_refused_by_the_device(self):
        controller = await self.start_controller()
        await controller.start_macro("correction.json")
        with self.assertRaises(Exception) as caught:
            await controller.place_figure("Zelda/Link.bin")
        self.assertEqual(getattr(caught.exception, "code", None).name, "BAD_STATE")
        self.assertEqual(controller.snapshot()["mode"], "MACRO")

    async def test_a_firmware_without_amiibo_is_a_distinct_refusal(self):
        device = StubDevice(features=0b001)
        controller = await self.start_controller(device=device)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["firmware"]["features"]["amiibo"], False)
        with self.assertRaises(ControllerError) as caught:
            await controller.place_figure("Zelda/Link.bin")
        self.assertEqual(caught.exception.code, "AMIIBO_UNSUPPORTED")

    async def test_a_reboot_is_recovery_new_power(self):
        device = StubDevice(boot_id=1)
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        device.reboot(boot_id=2)
        await controller.reconnect()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["recovery"], "NEW_POWER")
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertIsNone(snapshot["plan"])

    async def test_a_firmware_change_is_recovery_different_firmware(self):
        device = StubDevice()
        controller = await self.start_controller(device=device)
        device.hello_state = Hello(fw_major=2, boot_id=device.hello_state.boot_id)
        await controller.reconnect()
        self.assertEqual(controller.snapshot()["recovery"], "DIFFERENT_FIRMWARE")

    async def test_a_console_drop_is_consoles_lost_not_a_wire_code(self):
        controller = await self.start_controller()
        await controller.start_macro("correction.json")
        controller.device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if controller.snapshot()["console"]["lastDropReason"]:
                break
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["console"]["lastDropReason"], "531 (BLE_HS_ERR_HCI_BASE + 0x13)")
        self.assertEqual(snapshot["stopReason"], "CONSOLE_LOST")

    async def test_an_event_is_a_prompt_to_re_read_status(self):
        controller = await self.start_controller()
        controller._on_event(Event(EventKind.MODE_CHANGED, bytes([1])))
        await controller.refresh()
        self.assertEqual(controller.snapshot()["mode"], "IDLE")

    async def test_an_upload_can_be_abandoned(self):
        device = SlowStubDevice()
        controller = await self.start_controller(device=device)
        task = asyncio.create_task(controller.start_macro("correction.json"))
        await asyncio.sleep(0.03)
        self.assertIsNotNone(controller.snapshot()["uploading"])
        await controller.cancel_upload()
        with self.assertRaises(ControllerError) as caught:
            await task
        self.assertEqual(caught.exception.code, "UPLOAD_CANCELLED")
        self.assertIsNone(controller.snapshot()["uploading"])
        self.assertIsNone(controller.snapshot()["plan"])

    async def test_config_and_unpair_reach_the_device(self):
        device = StubDevice()
        controller = await self.start_controller(device=device)
        await controller.save_config(report_interval_ms=10, led=False)
        self.assertEqual(controller.snapshot()["config"], {"reportIntervalMs": 10, "led": False})
        await controller.pair_unpair()
        self.assertFalse(controller.snapshot()["console"]["bonded"])

    async def test_subscribe_starts_with_a_state_snapshot(self):
        controller = await self.start_controller()
        stream = controller.subscribe()
        name, payload = await anext(stream)
        self.assertEqual(name, "state")
        self.assertEqual(payload["mode"], "IDLE")
        await stream.aclose()

    async def test_rescan_does_not_reread_the_key(self):
        # §6.7: the key is read once at startup; finding it afterwards means a
        # restart. `Rescan` is for the libraries only.
        controller = await self.start_controller(with_key=False)
        self.assertEqual(controller.snapshot()["key"], "KEY_ABSENT")
        self.controller.keys.key_file.write_bytes(bytes(160))
        await controller.rescan()
        self.assertEqual(controller.snapshot()["key"], "KEY_ABSENT")

    async def test_a_plan_above_the_advertised_capacity_is_pre_checked(self):
        device = StubDevice()
        device.hello_state = Hello(plan_capacity_bytes=100, boot_id=device.hello_state.boot_id)
        controller = await self.start_controller(device=device)
        with self.assertRaises(ControllerError) as caught:
            await controller.start_macro("correction.json")
        self.assertEqual(caught.exception.code, "PLAN_TOO_LARGE")
        # Nothing reached the wire: the device has no committed plan.
        self.assertIsNone(controller.snapshot()["plan"])

    async def test_a_key_that_fails_to_seal_sets_key_invalid(self):
        def broken_sealer(image: bytes, key: KeyMaterial) -> SealedTag:
            from container.ns2sealing import KeyInvalid

            raise KeyInvalid("unpack round trip failed")

        settings = self.build_settings()
        self.controller = create_controller(settings, device=StubDevice(), sealer=broken_sealer)
        await self.controller.start()
        with self.assertRaises(ControllerError) as caught:
            await self.controller.place_figure("Zelda/Link.bin")
        self.assertEqual(caught.exception.code, "KEY_INVALID")
        self.assertEqual(self.controller.snapshot()["key"], "KEY_INVALID")
        self.assertEqual(self.controller.snapshot()["mode"], "IDLE")


if __name__ == "__main__":
    unittest.main()
