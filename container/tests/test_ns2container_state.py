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
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2container import ControllerError, Settings, create_controller  # noqa: E402
from container.ns2device import (  # noqa: E402
    ConsoleLink,
    Event,
    EventKind,
    Hello,
    StubDevice,
)
from container.ns2sealing import (  # noqa: E402
    KeyMaterial,
    SealedTag,
    SealingUnavailable,
    bcc0_for,
    identity_of,
)

TAG = bytes.fromhex("0411fe63ca526c81") + bytes(532)


def fake_sealer(image: bytes, key: KeyMaterial) -> SealedTag:
    """A test double, not a fallback: it copies the source image's identity."""
    return SealedTag(image=image, identity=identity_of(image))


class RotatingSealer:
    """A faithful double of the #32 seam: every seal mints a fresh identity.

    Rotation is only observable if the sealer never hands back the identity it
    was given (§6.1: one identity per placement), so the double mints
    `0x04` + a counter exactly like `mint_identity` mints `0x04` + CSPRNG.
    """

    def __init__(self) -> None:
        self.mints = 0

    def __call__(self, image: bytes, key: KeyMaterial) -> SealedTag:
        self.mints += 1
        identity = bytes([0x04]) + self.mints.to_bytes(6, "big")
        block = identity[:3] + bytes([bcc0_for(identity)]) + identity[3:]
        return SealedTag(image=block + image[8:], identity=identity)


class FlakySealer:
    """Seals once, then fails — a key that goes bad between placements."""

    def __init__(self) -> None:
        self.inner = RotatingSealer()
        self.failed = False

    def __call__(self, image: bytes, key: KeyMaterial) -> SealedTag:
        if self.failed:
            raise SealingUnavailable("the round trip broke between placements")
        return self.inner(image, key)


class CountingDevice(StubDevice):
    """Counts the verbs chapter 9's policy is allowed to send."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stop_calls = 0
        self.start_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()

    async def start(self) -> None:
        self.start_calls += 1
        await super().start()


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

    async def start_controller(self, *, device=None, with_key: bool = True, sealer=fake_sealer):
        settings = self.build_settings(with_key=with_key)
        self.controller = create_controller(
            settings,
            device=device if device is not None else StubDevice(),
            sealer=sealer,
        )
        await self.controller.start()
        return self.controller

    async def wait_for(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached within timeout")

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

    async def test_a_console_drop_mid_macro_stops_the_run_once(self):
        # §9.3: the container sends STOP and surfaces `Stopped: console
        # disconnected`. The device sees a plain CONTAINER_STOP — CONSOLE_LOST
        # is container-side only and never becomes a wire value.
        device = CountingDevice()
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["stopReason"] == "CONSOLE_LOST")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertEqual(device.stop_calls, 1)
        # The wire carries a plain CONTAINER_STOP — CONSOLE_LOST never reaches it.
        self.assertIs((await device.status()).last_stop_reason.name, "CONTAINER_STOP")
        # §4.3: a stop retains the plan — the run is one START away.
        self.assertIsNotNone(snapshot["plan"])
        self.assertIsNone(snapshot["lastError"])

    async def test_no_auto_restart_after_the_console_reconnects(self):
        # §9.1: the console re-initialises itself on wake, so the container has
        # nothing to do but wait for the human — exactly as after a BOOT_LOCAL.
        device = CountingDevice()
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["stopReason"] == "CONSOLE_LOST")
        starts_before_reconnect = device.start_calls
        device.set_console_link(ConsoleLink.CONNECTED)
        device.console_resubscribes()
        await asyncio.sleep(0.2)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertEqual(device.start_calls, starts_before_reconnect)
        # The story of why the run ended survives the reconnect.
        self.assertEqual(snapshot["stopReason"], "CONSOLE_LOST")
        self.assertEqual(snapshot["console"]["link"], "CONNECTED")

    async def test_a_manual_stop_after_a_drop_reads_container_stop(self):
        device = CountingDevice()
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["stopReason"] == "CONSOLE_LOST")
        device.set_console_link(ConsoleLink.CONNECTED)
        await controller.start_macro("correction.json")
        # The stop-reason surface is read where the UI renders it — in IDLE
        # after the run ends — not mid-run (§3.4: a START does not clear it).
        await controller.stop_macro()
        self.assertEqual(controller.snapshot()["stopReason"], "CONTAINER_STOP")

    async def test_a_console_drop_keeps_a_placement_and_rotates_on_reconnect(self):
        # §9.3: a placed tag nobody reads is harmless, but the console must not
        # re-scan an identity it already saw — rotate before its first scan.
        sealer = RotatingSealer()
        controller = await self.start_controller(sealer=sealer)
        await controller.place_figure("Zelda/Link.bin")
        before = controller.snapshot()["placement"]
        device = controller.device
        device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["console"]["lastDropReason"] is not None)
        # The placement survived the drop untouched.
        kept = controller.snapshot()["placement"]
        self.assertEqual(kept["identity"], before["identity"])
        self.assertEqual(controller.snapshot()["mode"], "AMIIBO")
        device.set_console_link(ConsoleLink.CONNECTED)
        await self.wait_for(
            lambda: (controller.snapshot()["placement"] or {}).get("identity") != before["identity"]
        )
        rotated = controller.snapshot()["placement"]
        self.assertEqual(rotated["index"], 2)
        self.assertEqual(rotated["scans"], 0)
        # §9.1: the wake is a reconnect *and* a re-subscribe — one rotation, not two.
        device.console_resubscribes()
        await asyncio.sleep(0.2)
        self.assertEqual(controller.snapshot()["placement"]["index"], 2)
        self.assertEqual(sealer.mints, 2)

    async def test_a_missed_connected_edge_still_rotates_on_resubscribed(self):
        # The container may attach after the CONNECTED edge; the re-subscribe
        # is the other edge §9.1 promises, and the rotation is idempotent per
        # drop either way.
        sealer = RotatingSealer()
        controller = await self.start_controller(sealer=sealer)
        await controller.place_figure("Zelda/Link.bin")
        before = controller.snapshot()["placement"]["identity"]
        controller.device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["console"]["lastDropReason"] is not None)
        controller.device.console_resubscribes()
        await self.wait_for(
            lambda: (controller.snapshot()["placement"] or {}).get("identity") != before
        )

    async def test_a_scan_ended_rotates_the_placement(self):
        # §6.5/§3.5: SCAN_ENDED mints the next identity and pushes it, so a
        # virgin tag is staged before the next scan — the same action the
        # reconnect policy takes, not new machinery.
        sealer = RotatingSealer()
        controller = await self.start_controller(sealer=sealer)
        await controller.place_figure("Zelda/Link.bin")
        before = controller.snapshot()["placement"]["identity"]
        controller.device.console_ends_scan()
        await self.wait_for(
            lambda: (controller.snapshot()["placement"] or {}).get("identity") != before
        )
        rotated = controller.snapshot()["placement"]
        self.assertEqual(rotated["index"], 2)
        self.assertEqual(controller.snapshot()["mode"], "AMIIBO")

    async def test_a_rotation_failure_is_a_warning_not_a_crash(self):
        sealer = FlakySealer()
        controller = await self.start_controller(sealer=sealer)
        await controller.place_figure("Zelda/Link.bin")
        before = controller.snapshot()["placement"]["identity"]
        sealer.failed = True
        controller.device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["console"]["lastDropReason"] is not None)
        controller.device.set_console_link(ConsoleLink.CONNECTED)
        await asyncio.sleep(0.2)
        # The placement is kept as placed; the failure is on the Logs screen.
        self.assertEqual(controller.snapshot()["placement"]["identity"], before)
        self.assertTrue(any("rotat" in line.message.lower() for line in controller._logs))
        # And the event loop is still alive: a later edge still reaches the device.
        sealer.failed = False
        controller.device.console_resubscribes()
        await self.wait_for(
            lambda: (controller.snapshot()["placement"] or {}).get("identity") != before
        )

    async def test_a_console_drop_while_idle_sends_nothing(self):
        device = CountingDevice()
        controller = await self.start_controller(device=device)
        device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await self.wait_for(lambda: controller.snapshot()["console"]["lastDropReason"] is not None)
        await asyncio.sleep(0.1)
        self.assertEqual(device.stop_calls, 0)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertEqual(snapshot["stopReason"], "NONE")

    async def test_a_reboot_mid_macro_ends_the_run_without_inventing_a_reason(self):
        # §9.2: a reboot during a macro silently ends the run — mode=IDLE with
        # last_stop_reason=NONE. Normal operation, not an error path.
        device = StubDevice(boot_id=1)
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        device.reboot(boot_id=2)
        await self.wait_for(lambda: controller.snapshot()["recovery"] == "NEW_POWER")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["mode"], "IDLE")
        self.assertEqual(snapshot["stopReason"], "NONE")
        self.assertIsNone(snapshot["lastError"])
        self.assertIsNone(snapshot["plan"])

    async def test_starting_with_no_console_is_allowed(self):
        # §9.3: refusing would be dishonest about a dependency that does not
        # exist — the device does not need the console to run a macro.
        device = StubDevice(console_link=ConsoleLink.ADVERTISING)
        controller = await self.start_controller(device=device)
        self.assertEqual(controller.snapshot()["console"]["link"], "ADVERTISING")
        await controller.start_macro("correction.json")
        self.assertEqual(controller.snapshot()["mode"], "MACRO")
        await controller.stop_macro()
        await controller.place_figure("Zelda/Link.bin")
        self.assertEqual(controller.snapshot()["mode"], "AMIIBO")

    async def test_starting_with_no_console_warns_that_it_runs_into_the_void(self):
        # G-4 is observed, not assumed: the run is *allowed* and the container
        # *warns*, because the one consequence worth stating is that nothing will
        # look different until a console connects (§9.3).
        device = StubDevice(console_link=ConsoleLink.ADVERTISING)
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        warnings = [line.message for line in controller._logs
                    if line.level == "warn" and line.source == "container"]
        self.assertTrue(any("not connected" in message for message in warnings),
                        f"no console warning among {warnings!r}")
        self.assertEqual(controller.snapshot()["mode"], "MACRO",
                         "the warning does not refuse the run")
        await controller.stop_macro()

    async def test_starting_with_a_connected_console_does_not_warn(self):
        device = StubDevice(console_link=ConsoleLink.CONNECTED)
        controller = await self.start_controller(device=device)
        await controller.start_macro("correction.json")
        warnings = [line.message for line in controller._logs
                    if line.level == "warn" and line.source == "container"]
        self.assertFalse(any("not connected" in message for message in warnings),
                         f"a connected console still warned: {warnings!r}")
        await controller.stop_macro()

    async def test_placing_with_no_console_warns_too(self):
        # §9.3 names `PLACE_AMIIBO` in the same sentence as `START`: the placement
        # is allowed, and the console is what is missing.
        device = StubDevice(console_link=ConsoleLink.ADVERTISING)
        controller = await self.start_controller(device=device)
        await controller.place_figure("Zelda/Link.bin")
        warnings = [line.message for line in controller._logs
                    if line.level == "warn" and line.source == "container"]
        self.assertTrue(any("not connected" in message for message in warnings),
                        f"no console warning among {warnings!r}")
        self.assertEqual(controller.snapshot()["mode"], "AMIIBO",
                         "the warning does not refuse the placement")

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
