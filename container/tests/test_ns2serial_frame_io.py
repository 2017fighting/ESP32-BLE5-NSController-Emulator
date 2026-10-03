#!/usr/bin/env python3
"""`FrameTransport`: the §2.3 conversation and the §2.7 bulk stream (#30).

The container side here is the real `FrameTransport`; the other side is the
byte-level `FakeBoard`, so every byte crosses a real COBS/CRC encode/decode.
What is pinned:

- one outstanding control request, enforced by the lock rather than by hoping;
- the ACK window keeps at most 4096 un-ACKed bytes in flight (§2.7, ADR-0015);
- a dropped ACK resumes from the ACK'd offset, at window granularity;
- an `ERROR` reply becomes a typed `CommandError`, never a silent payload;
- `EVENT` frames and `ESP_LOG` noise are demuxed from the same bytes;
- a dead link wakes the waiter instead of hanging it, and the next request
  reopens the port.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2serial_frame_io*.py'
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fake_board import ACK_WINDOW, FakeBoard, pipe  # noqa: E402

from container.ns2device import CommandError, ErrorCode, EventKind, Mode, Verb  # noqa: E402
from container.ns2serial import FrameTransport, TransportUnavailable  # noqa: E402


class FrameTransportTest(unittest.IsolatedAsyncioTestCase):
    async def make_pair(self, **kwargs):
        host, device = pipe()
        board = FakeBoard(**kwargs)
        board.transport = device
        board.start()
        self.board = board
        self.io = FrameTransport(host, request_timeout=0.5, bulk_timeout=0.25, bulk_retries=3)
        await self.io.open()
        return self.io, board

    async def asyncTearDown(self):
        if getattr(self, "io", None) is not None:
            await self.io.close()
        if getattr(self, "board", None) is not None:
            await self.board.stop()

    async def test_hello_and_status_round_trip(self):
        io, _ = await self.make_pair()
        hello = await io.request(Verb.HELLO, bytes([1]))
        self.assertEqual(len(hello), 20)
        status = await io.request(Verb.STATUS)
        self.assertEqual(len(status), 47)

    async def test_an_error_reply_is_a_typed_command_error(self):
        io, board = await self.make_pair()
        board.error_on[int(Verb.STATUS)] = (ErrorCode.BAD_STATE, int(Mode.MACRO))
        with self.assertRaises(CommandError) as caught:
            await io.request(Verb.STATUS)
        self.assertEqual(caught.exception.code, ErrorCode.BAD_STATE)
        self.assertEqual(caught.exception.detail, int(Mode.MACRO))

    async def test_only_one_control_request_is_outstanding(self):
        io, board = await self.make_pair()
        board.delay_reply = 0.02
        await asyncio.gather(io.request(Verb.HELLO, bytes([1])), io.request(Verb.STATUS))
        self.assertEqual(board.max_in_flight, 1)

    async def test_bulk_windows_the_chunks_and_commits(self):
        io, board = await self.make_pair()
        payload = bytes(range(256)) * 20  # 5120 B, more than one 4096 B window
        digest = b"\xab" * 16
        await io.bulk(Verb.LOAD_PLAN, payload, plan_hash=digest)
        self.assertEqual(board.plan, payload)
        self.assertEqual(board.plan_hash, digest)
        self.assertLessEqual(board.max_unacked, ACK_WINDOW)
        self.assertGreater(board.max_unacked, 0)

    async def test_bulk_resumes_from_the_acked_offset_after_a_lost_ack(self):
        io, board = await self.make_pair()
        board.drop_acks = 1
        payload = bytes(range(256)) * 20
        await io.bulk(Verb.LOAD_PLAN, payload, plan_hash=b"\x01" * 16)
        self.assertEqual(board.plan, payload)
        self.assertEqual(board.drop_acks, 0)

    async def test_bulk_reports_progress_to_the_whole_transfer(self):
        io, _ = await self.make_pair()
        seen: list[tuple[int, int]] = []
        await io.bulk(
            Verb.LOAD_PLAN,
            bytes(300),
            plan_hash=b"\x02" * 16,
            on_progress=lambda offset, total: seen.append((offset, total)),
        )
        self.assertEqual(seen[-1], (300, 300))
        self.assertEqual(seen[0], (0, 300))

    async def test_a_bulk_announce_error_propagates_and_changes_nothing(self):
        io, board = await self.make_pair()
        board.error_on[int(Verb.LOAD_PLAN)] = (ErrorCode.BAD_STATE, int(Mode.MACRO))
        with self.assertRaises(CommandError) as caught:
            await io.bulk(Verb.LOAD_PLAN, bytes(300), plan_hash=b"\x03" * 16)
        self.assertEqual(caught.exception.code, ErrorCode.BAD_STATE)
        self.assertIsNone(board.plan)

    async def test_events_are_demuxed_from_the_same_stream(self):
        io, board = await self.make_pair()
        await io.request(Verb.STATUS)  # open the link and start the reader
        stream = io.events()
        board.emit(EventKind.MODE_CHANGED, bytes([int(Mode.MACRO)]))
        event = await asyncio.wait_for(anext(stream), 1.0)
        self.assertEqual(event.kind, EventKind.MODE_CHANGED)
        self.assertEqual(event.mode, Mode.MACRO)
        await stream.aclose()

    async def test_log_text_is_demuxed_instead_of_counted_as_a_dropped_frame(self):
        io, board = await self.make_pair()
        await io.request(Verb.STATUS)  # open the link and start the reader
        stream = io.log_lines()
        board.send_log("I (123) wifi: connected")
        board.send_log("E (456) gap: boom")
        self.assertEqual(await asyncio.wait_for(anext(stream), 1.0), ("info", "wifi: connected"))
        self.assertEqual(await asyncio.wait_for(anext(stream), 1.0), ("error", "gap: boom"))
        await stream.aclose()

    async def test_a_dead_link_wakes_a_pending_request(self):
        io, board = await self.make_pair()
        board.delay_reply = 1.0
        task = asyncio.create_task(io.request(Verb.HELLO, bytes([1])))
        await asyncio.sleep(0.02)
        await board.transport.close()
        with self.assertRaises(TransportUnavailable):
            await asyncio.wait_for(task, 1.0)

    async def test_the_next_request_reopens_a_dead_link(self):
        io, board = await self.make_pair()
        board.delay_reply = 1.0
        task = asyncio.create_task(io.request(Verb.HELLO, bytes([1])))
        await asyncio.sleep(0.02)
        await board.transport.close()
        with self.assertRaises(TransportUnavailable):
            await task

        board.delay_reply = 0.0
        await board.transport.open()  # the board is replugged
        board.start()
        await io.open()
        status = await asyncio.wait_for(io.request(Verb.STATUS), 1.0)
        self.assertEqual(len(status), 47)


if __name__ == "__main__":
    unittest.main()
