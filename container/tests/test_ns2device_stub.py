#!/usr/bin/env python3
"""The device stub holds §4.3's transition table and §2.5's rejections.

The stub exists so the following ticket has a contract to satisfy: these tests
pin the rejections (`NO_PLAN`, `ALREADY_RUNNING`, `BAD_STATE`, `BAD_PLAN`), the
retention rules (a stop keeps the plan, a long BOOT drops it), the plan-hash
comparison (§5.6) and the §3.3 event kinds.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2device*.py'
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

from container.ns2device import (  # noqa: E402
    CommandError,
    ConsoleLink,
    ErrorCode,
    EventKind,
    Hello,
    Mode,
    Status,
    StubDevice,
)
from container.ns2plan import load_plan  # noqa: E402


def _plan():
    return load_plan(support.correction_macro_path())


def _zero_record_plan() -> bytes:
    header = struct.pack("<IBBHI", 0x4C50534E, 1, 11, 0, 0)
    return header


class WireStructs(unittest.TestCase):
    def test_hello_is_twenty_bytes_and_round_trips(self):
        hello = Hello(boot_id=0xDEADBEEF)
        payload = hello.to_bytes()
        self.assertEqual(len(payload), 20)
        self.assertEqual(Hello.from_bytes(payload), hello)

    def test_status_is_forty_seven_bytes_and_round_trips(self):
        status = Status(mode=Mode.MACRO, plan_hash=b"\x11" * 16, tag_identity=b"\x22" * 7)
        payload = status.to_bytes()
        self.assertEqual(len(payload), 47)
        self.assertEqual(Status.from_bytes(payload), status)

    def test_status_length_disagreement_is_a_protocol_error(self):
        with self.assertRaises(ValueError):
            Status.from_bytes(b"\x00" * 46)


class Transitions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = StubDevice()
        self.plan = _plan()

    async def load(self):
        await self.device.load_plan(self.plan.identity, self.plan.payload)

    async def test_start_with_no_plan_is_no_plan(self):
        with self.assertRaises(CommandError) as caught:
            await self.device.start()
        self.assertEqual(caught.exception.code, ErrorCode.NO_PLAN)

    async def test_load_commits_and_start_enters_macro(self):
        await self.load()
        status = await self.device.status()
        self.assertIs(status.mode, Mode.IDLE)
        self.assertEqual(status.plan_hash, self.plan.identity)
        self.assertEqual(status.plan_frame_count, self.plan.record_count)
        await self.device.start()
        self.assertIs((await self.device.status()).mode, Mode.MACRO)

    async def test_start_twice_is_already_running(self):
        await self.load()
        await self.device.start()
        with self.assertRaises(CommandError) as caught:
            await self.device.start()
        self.assertEqual(caught.exception.code, ErrorCode.ALREADY_RUNNING)

    async def test_a_hash_mismatch_is_bad_plan(self):
        with self.assertRaises(CommandError) as caught:
            await self.device.load_plan(b"\x00" * 16, self.plan.payload)
        self.assertEqual(caught.exception.code, ErrorCode.BAD_PLAN)

    async def test_a_plan_too_large_for_capacity_is_refused(self):
        oversized = b"\x00" * (self.device.hello_state.plan_capacity_bytes + 1)
        with self.assertRaises(CommandError) as caught:
            await self.device.load_plan(b"\x00" * 16, oversized)
        self.assertEqual(caught.exception.code, ErrorCode.PLAN_TOO_LARGE)

    async def test_a_zero_record_plan_commits_but_start_refuses_it(self):
        payload = _zero_record_plan()
        identity = hashlib.sha256(payload).digest()[:16]
        await self.device.load_plan(identity, payload)
        with self.assertRaises(CommandError) as caught:
            await self.device.start()
        self.assertEqual(caught.exception.code, ErrorCode.BAD_PLAN)
        self.assertIs((await self.device.status()).mode, Mode.IDLE)

    async def test_placing_while_macro_is_bad_state(self):
        await self.load()
        await self.device.start()
        with self.assertRaises(CommandError) as caught:
            await self.device.place(bytes(540))
        self.assertEqual(caught.exception.code, ErrorCode.BAD_STATE)
        self.assertIs((await self.device.status()).mode, Mode.MACRO)

    async def test_loading_while_macro_is_bad_state(self):
        await self.load()
        await self.device.start()
        with self.assertRaises(CommandError) as caught:
            await self.device.load_plan(self.plan.identity, self.plan.payload)
        self.assertEqual(caught.exception.code, ErrorCode.BAD_STATE)

    async def test_stop_keeps_the_plan_and_records_a_container_stop(self):
        await self.load()
        await self.device.start()
        await self.device.stop()
        status = await self.device.status()
        self.assertIs(status.mode, Mode.IDLE)
        self.assertEqual(status.plan_frame_count, self.plan.record_count)
        self.assertEqual(status.last_stop_reason.name, "CONTAINER_STOP")

    async def test_a_placed_tag_carries_its_uid(self):
        tag = bytearray(540)
        tag[0:8] = bytes.fromhex("0411fe63ca526c81")
        await self.device.place(bytes(tag))
        status = await self.device.status()
        self.assertIs(status.mode, Mode.AMIIBO)
        self.assertEqual(status.tag_identity.hex(), "0411feca526c81")
        await self.device.unplace()
        self.assertIs((await self.device.status()).mode, Mode.IDLE)
        # Idempotent in IDLE.
        await self.device.unplace()

    async def test_a_bad_tag_length_is_bad_length(self):
        with self.assertRaises(CommandError) as caught:
            await self.device.place(bytes(100))
        self.assertEqual(caught.exception.code, ErrorCode.BAD_LENGTH)

    async def test_a_short_boot_stop_keeps_the_plan_and_a_long_one_drops_it(self):
        await self.load()
        await self.device.start()
        self.device.boot_button(long=False)
        status = await self.device.status()
        self.assertIs(status.mode, Mode.IDLE)
        self.assertEqual(status.last_stop_reason.name, "BOOT_LOCAL")
        self.assertIs(status.plan_state.name, "COMMITTED")
        self.device.boot_button(long=True)
        self.assertIs((await self.device.status()).plan_state.name, "NONE")

    async def test_reboot_clears_plan_and_tag(self):
        await self.load()
        self.device.reboot(boot_id=0x1234)
        status = await self.device.status()
        self.assertEqual(status.plan_state.name, "NONE")
        self.assertEqual(status.tag_state.name, "NONE")
        self.assertEqual(self.device.hello_state.boot_id, 0x1234)

    async def test_progress_ends_at_total(self):
        seen: list[tuple[int, int]] = []
        await self.device.load_plan(
            self.plan.identity, self.plan.payload, on_progress=lambda o, t: seen.append((o, t))
        )
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (self.plan.size, self.plan.size))

    async def test_unpair_clears_the_bond(self):
        await self.device.pair_unpair()
        self.assertEqual((await self.device.status()).bond.name, "UNPAIRED")

    async def test_events_are_emitted_for_edges(self):
        stream = self.device.events()
        kinds: list[EventKind] = []

        async def collect():
            async for event in stream:
                kinds.append(event.kind)
                if event.kind is EventKind.PLAN_COMMITTED:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)  # let the subscription register
        await self.load()
        await asyncio.wait_for(task, timeout=1)
        self.assertIn(EventKind.PLAN_COMMITTED, kinds)

    async def test_console_link_edge_carries_the_reason(self):
        stream = self.device.events()
        seen: list[tuple] = []

        async def collect():
            async for event in stream:
                edge = event.link_edge
                if edge is not None:
                    seen.append(edge)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        self.device.set_console_link(ConsoleLink.ADVERTISING, reason=531)
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(seen[0][1], 531)


if __name__ == "__main__":
    unittest.main()
