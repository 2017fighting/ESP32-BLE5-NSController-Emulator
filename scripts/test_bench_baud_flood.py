#!/usr/bin/env python3
"""Tests for `bench_baud_flood.py` — the G-1 measurement's counting uploader.

Run:  python3 scripts/test_bench_baud_flood.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only (the repo's rule for `scripts/` tests). The object under
test is `upload_plan_counted`: the synchronous mirror of the container's
`FrameIO.bulk` (`container/ns2serial/frame_io.py`), which is the deployment's
real windowed uploader — same window fill, same resume-from-the-ACK'd-offset
retry at window granularity, same commit. What the tests pin is the *accounting*
the bench reports (window retries, resent bytes, duplicate/early ACKs) and the
§2.7 semantics the fake device encodes:

- a chunk whose `offset != next_expected` draws an ACK of `next_expected` and
  **no write** (resume is offset-keyed, §2.7 rule 5);
- the device ACKs at the 4096-byte window boundary and on completion, and **may
  ACK earlier** (§2.7: the container must never depend on boundary ACKs);
- a frame the device cannot trust is *silent* (§2.8), which is how a dropped
  chunk manifests: the next chunk draws the offset-mismatch ACK instead;
- an `ERROR` is a REPLY with verb 10 and aborts the transfer.

The fake is deliberately not the firmware — `container/tests/fake_board.py` is
the byte-level double; this one is the rule set the retry accounting hangs on.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import unittest
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_plan_executor import (  # noqa: E402
    ACK_WINDOW,
    BULK_ANNOUNCE,
    BULK_CHUNK,
    BULK_COMMIT,
    CHUNK_SIZE,
    VERB_ERROR,
    VERB_LOAD_PLAN,
)
from bench_baud_flood import (  # noqa: E402
    CommandError,
    UploadStats,
    synthesize_plan,
    upload_plan_counted,
)

# container/ns2device/model.py's ErrorCode.PLAN_TOO_LARGE, quoted locally so
# the fake's fabricated refusal needs no container import.
PLAN_TOO_LARGE = 6


class FakeDevice:
    """The §2.7 bulk rules, synchronously, with scriptable frame loss.

    Presents exactly the link surface `upload_plan_counted` uses:
    `request`, `send`, `wait_reply`. `drop_chunk_offsets` silently discards the
    chunk frame at each byte offset once (an isolated corruption — the resend
    arrives intact); `drop_ack_frames` swallows the first N bulk ACKs
    (device→host loss); `ack_every_chunk` exercises §2.7's "may ACK earlier".
    """

    def __init__(self, *, window: int = ACK_WINDOW, ack_every_chunk: bool = False):
        self.window = window
        self.ack_every_chunk = ack_every_chunk
        self.drop_chunk_offsets: set[int] = set()
        self.drop_ack_frames = 0
        self.replies: deque = deque()
        self.next_expected = 0
        self.acked = 0
        self.total = 0
        self.stage_open = False
        self.writes: Counter[int] = Counter()  # chunk offset -> frames written
        self.bytes_written = 0
        self.commits = 0

    # ── the link-facing surface ────────────────────────────────────────────

    def request(self, verb: int, payload: bytes = b"", *, timeout: float = 0.5):
        self.send(verb, payload)
        return self.wait_reply(verb, timeout)

    def send(self, verb: int, payload: bytes) -> None:
        op = payload[0]
        if op == BULK_ANNOUNCE:
            (self.total,) = struct.unpack_from("<I", payload, 1)
            self.stage_open = True
            self.next_expected = self.acked = 0
            self._ack(0, force=True)  # the announce ACK is never droppable
        elif op == BULK_CHUNK:
            (offset,) = struct.unpack_from("<I", payload, 1)
            if offset in self.drop_chunk_offsets:
                self.drop_chunk_offsets.discard(offset)  # dropped once
                return  # §2.8: an untrusted frame is silent
            data = payload[5:]
            if offset != self.next_expected:
                self._ack(self.next_expected)  # offset-keyed resume, no write
                return
            self.next_expected += len(data)
            self.writes[offset] += 1
            self.bytes_written += len(data)
            if self.ack_every_chunk or self.next_expected - self.acked >= self.window \
                    or self.next_expected == self.total:
                self.acked = self.next_expected
                self._ack(self.acked)
        elif op == BULK_COMMIT:
            (total,) = struct.unpack_from("<I", payload, 1)
            assert total == self.total and self.next_expected == self.total, "bad commit"
            self.commits += 1
            self.stage_open = False
            self._ack(self.total)

    def wait_reply(self, verb: int, timeout: float):
        if not self.replies:
            raise TimeoutError("no reply")
        frame_verb, payload = self.replies.popleft()
        if frame_verb == VERB_ERROR:
            return _Frame(VERB_ERROR, payload)
        assert frame_verb == verb, f"reply for verb {frame_verb}, waiting for {verb}"
        return _Frame(frame_verb, payload)

    # ── internals ──────────────────────────────────────────────────────────

    def _ack(self, offset: int, *, force: bool = False) -> None:
        if not force and self.drop_ack_frames > 0:
            self.drop_ack_frames -= 1
            return
        self.replies.append((VERB_LOAD_PLAN, struct.pack("<I", offset)))


class _Frame:
    __slots__ = ("verb", "payload")

    def __init__(self, verb: int, payload: bytes):
        self.verb = verb
        self.payload = payload


class RefusingDevice(FakeDevice):
    """Answers the announce with `ERROR PLAN_TOO_LARGE`, like the firmware."""

    def send(self, verb: int, payload: bytes) -> None:
        if payload[0] == BULK_ANNOUNCE:
            self.replies.append((VERB_ERROR, bytes([PLAN_TOO_LARGE]) + struct.pack("<I", 65537)))
            return
        super().send(verb, payload)


class SynthesizePlan(unittest.TestCase):
    def test_header_matches_plan_h_and_identity_is_sha256_16(self):
        for records in (0, 48, 5955):
            payload, identity = synthesize_plan(records)
            magic, version, record_size, count, loop_ms = struct.unpack_from("<IBBHI", payload)
            self.assertEqual(magic.to_bytes(4, "little"), b"NSPL")
            self.assertEqual((version, record_size, count), (1, 11, records))
            self.assertEqual(len(payload), 12 + 11 * records)
            self.assertEqual(identity, hashlib.sha256(payload).digest()[:16])

    def test_the_largest_plan_fits_the_advertised_capacity(self):
        # 65536 B capacity; 12 + 11·n ≤ 65536 ⇒ n ≤ 5956.7 ⇒ 5956 is the max.
        payload, _ = synthesize_plan(5956)
        self.assertEqual(len(payload), 65528)
        self.assertLessEqual(len(payload), 65536)
        self.assertEqual(12 + 11 * 5957, 65539)  # one record more cannot fit


class UploadPlanCounted(unittest.TestCase):
    def setUp(self):
        self.plan, self.identity = synthesize_plan(1200)  # 13,212 B ≈ 3.2 windows

    def stats(self) -> UploadStats:
        return UploadStats()

    def test_clean_upload_has_zero_retries_and_writes_each_byte_once(self):
        device = FakeDevice()
        stats = upload_plan_counted(device, self.plan, self.identity,
                                    ack_timeout=0.05, stats=self.stats())
        self.assertEqual(stats.window_retries, 0)
        self.assertEqual(stats.resent_bytes, 0)
        self.assertEqual(stats.dup_acks, 0)
        self.assertEqual(stats.chunk_frames, (len(self.plan) + CHUNK_SIZE - 1) // CHUNK_SIZE)
        self.assertEqual(device.commits, 1)
        self.assertEqual(device.bytes_written, len(self.plan))
        self.assertEqual(sorted(device.writes), list(range(0, len(self.plan), CHUNK_SIZE)))
        self.assertTrue(all(n == 1 for n in device.writes.values()))
        self.assertFalse(device.replies)

    def test_a_dropped_chunk_costs_exactly_one_window_retry(self):
        device = FakeDevice()
        device.drop_chunk_offsets = {2560}  # mid first window
        stats = upload_plan_counted(device, self.plan, self.identity,
                                    ack_timeout=0.05, stats=self.stats())
        self.assertEqual(stats.window_retries, 1)
        self.assertEqual(stats.resent_bytes, ACK_WINDOW)
        self.assertGreater(stats.dup_acks, 0)  # the offset-mismatch ACKs that follow
        self.assertEqual(device.commits, 1)
        self.assertEqual(device.bytes_written, len(self.plan))
        self.assertTrue(all(n == 1 for n in device.writes.values()))

    def test_a_lost_ack_costs_one_window_retry_and_no_double_write(self):
        device = FakeDevice()
        device.drop_ack_frames = 1
        stats = upload_plan_counted(device, self.plan, self.identity,
                                    ack_timeout=0.05, stats=self.stats())
        self.assertEqual(stats.window_retries, 1)
        self.assertEqual(stats.resent_bytes, ACK_WINDOW)
        self.assertEqual(device.bytes_written, len(self.plan))
        self.assertTrue(all(n == 1 for n in device.writes.values()))

    def test_an_ack_every_chunk_device_completes_without_retries(self):
        # §2.7: the device may ACK early under ring pressure; the uploader must
        # treat that as progress, never as a fault.
        device = FakeDevice(ack_every_chunk=True)
        stats = upload_plan_counted(device, self.plan, self.identity,
                                    ack_timeout=0.05, stats=self.stats())
        self.assertEqual(stats.window_retries, 0)
        self.assertGreater(stats.early_acks, 0)
        self.assertEqual(device.commits, 1)

    def test_exhausted_retries_raise(self):
        device = FakeDevice()
        # Re-drop the same offset on every attempt: a persistent fault, not an
        # isolated corruption, so the uploader must give up, not spin.
        class AlwaysDrop(FakeDevice):
            def send(self, verb, payload):
                if payload[0] == BULK_CHUNK and struct.unpack_from("<I", payload, 1)[0] == 2560:
                    return
                super().send(verb, payload)

        device = AlwaysDrop()
        with self.assertRaises(TimeoutError):
            upload_plan_counted(device, self.plan, self.identity,
                                ack_timeout=0.02, max_retries=2, stats=self.stats())
        self.assertEqual(device.commits, 0)

    def test_an_announce_error_aborts_before_any_chunk(self):
        device = RefusingDevice()
        with self.assertRaises(CommandError) as ctx:
            upload_plan_counted(device, self.plan, self.identity,
                                ack_timeout=0.05, stats=self.stats())
        self.assertEqual(ctx.exception.code, PLAN_TOO_LARGE)
        self.assertEqual(ctx.exception.detail, 65537)
        self.assertEqual(device.writes, Counter())


if __name__ == "__main__":
    unittest.main()
