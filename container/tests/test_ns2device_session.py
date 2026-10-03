#!/usr/bin/env python3
"""`SessionDevice`: the verbs over the real `FrameIO` (#30, §8.2).

The stub tests (#29) pin the transition table; these pin the wire contract the
real session adds on top of it:

- `HELLO` is exactly 20 bytes and `STATUS` exactly 47, or it is a `BAD_LENGTH`;
- `LOAD_PLAN` compares the device's echoed identity against the hash it sent and
  refuses to render a mismatched run as current (§5.6);
- the capacity check is container-side before anything reaches the wire (§8.4);
- `CONFIG` is the §2.9 three-byte payload.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2device_session*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fake_board import FakeBoard, pipe  # noqa: E402

from container.ns2device import (  # noqa: E402
    CommandError,
    ErrorCode,
    FEATURE_MACRO,
    PlanHashMismatch,
    ProtocolError,
    SessionDevice,
    Verb,
)
from container.ns2serial import FrameTransport  # noqa: E402


class SessionDeviceTest(unittest.IsolatedAsyncioTestCase):
    async def make_session(self, **kwargs):
        host, device = pipe()
        board = FakeBoard(**kwargs)
        board.transport = device
        board.start()
        self.board = board
        self.io = FrameTransport(host, request_timeout=0.5, bulk_timeout=0.25)
        self.device = SessionDevice(self.io)
        await self.io.open()
        return self.device, board

    async def asyncTearDown(self):
        if getattr(self, "io", None) is not None:
            await self.io.close()
        if getattr(self, "board", None) is not None:
            await self.board.stop()

    async def test_hello_carries_the_capabilities(self):
        device, _ = await self.make_session(boot_id=0xABCDEF01)
        hello = await device.hello()
        self.assertEqual(hello.proto_ver, 1)
        self.assertEqual(hello.boot_id, 0xABCDEF01)
        self.assertTrue(hello.supports(FEATURE_MACRO))

    async def test_a_hello_that_is_not_twenty_bytes_is_a_protocol_error(self):
        device, board = await self.make_session()
        board._hello = lambda frame: board._reply(Verb.HELLO, bytes(19))
        with self.assertRaises(ProtocolError):
            await device.hello()

    async def test_a_status_that_is_not_forty_seven_bytes_is_a_protocol_error(self):
        device, board = await self.make_session()
        board._status = lambda frame: board._reply(Verb.STATUS, bytes(46))
        with self.assertRaises(ProtocolError):
            await device.status()

    async def test_load_plan_compares_the_echoed_hash(self):
        device, board = await self.make_session()
        await device.hello()
        payload = bytes(range(256)) * 4
        digest = b"\x5a" * 16
        await device.load_plan(digest, payload)
        self.assertEqual(board.plan, payload)
        self.assertEqual(board.plan_hash, digest)

    async def test_a_mismatched_echoed_hash_is_surfaced_not_run(self):
        device, board = await self.make_session()
        await device.hello()
        payload = bytes(range(256)) * 4
        board.hash_override = b"\x00" * 16  # the board echoes something else
        with self.assertRaises(PlanHashMismatch) as caught:
            await device.load_plan(b"\x5a" * 16, payload)
        self.assertEqual(caught.exception.expected, b"\x5a" * 16)
        self.assertEqual(caught.exception.echoed, b"\x00" * 16)

    async def test_a_plan_above_the_advertised_capacity_never_reaches_the_wire(self):
        device, board = await self.make_session(plan_capacity_bytes=100)
        await device.hello()
        with self.assertRaises(CommandError) as caught:
            await device.load_plan(b"\x11" * 16, bytes(200))
        self.assertEqual(caught.exception.code, ErrorCode.PLAN_TOO_LARGE)
        self.assertNotIn(int(Verb.LOAD_PLAN), [verb for verb, _ in board.received])

    async def test_start_and_stop_move_the_device(self):
        device, _board = await self.make_session()
        await device.hello()
        await device.load_plan(b"\x22" * 16, bytes(300))
        await device.start()
        self.assertEqual((await device.status()).mode.name, "MACRO")
        await device.stop()
        self.assertEqual((await device.status()).mode.name, "IDLE")

    async def test_place_sends_the_540_bytes_on_the_bulk_path(self):
        device, board = await self.make_session()
        await device.hello()
        tag = bytes(range(256)) * 2 + bytes(28)
        self.assertEqual(len(tag), 540)
        await device.place(tag)
        self.assertEqual(board.tag, tag)
        self.assertEqual((await device.status()).tag_state.name, "PLACED")

    async def test_config_is_three_bytes_little_endian(self):
        device, board = await self.make_session()
        await device.config(report_interval_ms=25, led=False)
        verb, payload = board.received[-1]
        self.assertEqual(verb, int(Verb.CONFIG))
        self.assertEqual(payload, b"\x19\x00\x00")


if __name__ == "__main__":
    unittest.main()
