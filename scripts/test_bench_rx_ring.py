#!/usr/bin/env python3
"""Tests for `bench_rx_ring.py` — the #34 RX-ring / ACK-window measurement.

Run:  python3 scripts/test_bench_rx_ring.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only (the repo's rule for `scripts/` tests). Two things are
pinned, both against fakes rather than the firmware:

- **the ring-line contract.** The instrumented device prints one INFO line per
  bulk transfer once staging closes —
  `staging closed: total=65528B ring hw=255/256 spins=180842 backlog=232/256 drops=0`
  — and the bench must
  harvest it from the log noise the link already counts, attribute it to the
  transfer that caused it, and refuse to report a row whose line never arrived
  (a silent instrument is a finding, not a zero).
- **the verdict the ticket asks for.** #34 wants "a ring size and an ACK window
  stated with the stall count behind them": the verdict aggregates per-transfer
  rows into exactly that, and the two expectations encode the two kinds of row
  the bench runs — `clean` (the design point: any stall, any untrusted frame,
  any missing line is a failure) and `degenerate` (the 921600 positive control:
  stalls are the point, and the control *fails* if the meter reads no ring
  pressure, because then a clean 115200 reading would prove nothing).
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_plan_executor import (  # noqa: E402
    ACK_WINDOW,
    BULK_ANNOUNCE,
    BULK_CHUNK,
    BULK_COMMIT,
    MODE_IDLE,
    PLAN_COMMITTED,
    VERB_LOAD_PLAN,
    VERB_STATUS,
)
from bench_baud_flood import synthesize_plan  # noqa: E402
from bench_rx_ring import (  # noqa: E402
    load_real_plans,
    parse_ring_line,
    run_transfer,
    scan_ring_lines,
    verdict,
)


# ── the fake ──────────────────────────────────────────────────────────────────


class _Frame:
    __slots__ = ("verb", "payload")

    def __init__(self, verb: int, payload: bytes):
        self.verb = verb
        self.payload = payload


class RingDevice:
    """The §2.7 bulk rules plus the #34 instrument, synchronously.

    `test_bench_baud_flood.FakeDevice` is the rule set; this adds the two
    surfaces `run_transfer` also touches — a `STATUS` reply and the staging-
    close log line appended to `noise_log` exactly where the firmware prints
    it (as the commit reply is being written, i.e. ahead of it on the wire).
    The `ring_*` / `backlog_*` fields script what the meter read.
    """

    def __init__(self, *, window: int = ACK_WINDOW):
        self.window = window
        self.replies: deque = deque()
        self.next_expected = 0
        self.acked = 0
        self.total = 0
        self.commits = 0
        self.writes: Counter[int] = Counter()
        self.bytes_written = 0
        # the instrument's scriptable reading, and where it lands
        self.ring_hw = 255          # the blast's steady state, not a hazard
        self.ring_spins = 9000
        self.backlog_hw = 41        # ≪ capacity: the parser keeping pace
        self.backlog_capacity = 256
        self.ring_drops = 0         # frames the device could not trust
        self.ring_capacity = 256
        self.noise_log: list[bytes] = []
        self.emit_ring_line = True
        # the wire-level counters `run_transfer` quotes
        self.counters = {"bytes_rx": 0, "segments": 0, "noise": 0, "noise_bytes": 0,
                         "bad_crc": 0, "frames": 0}
        # what STATUS will claim after a commit (`force_status_hash` scripts a
        # lying echo — §8.5's never-trust rule is the bench's to check)
        self.status_plan_hash = None
        self.status_frame_count = 0
        self.force_status_hash = None

    # ── the link-facing surface ────────────────────────────────────────────

    def request(self, verb: int, payload: bytes = b"", *, timeout: float = 0.5):
        self.send(verb, payload)
        return self.wait_reply(verb, timeout)

    def send(self, verb: int, payload: bytes) -> None:
        if verb == VERB_STATUS:
            self.replies.append((VERB_STATUS, self._status_payload()))
            return
        op = payload[0]
        if op == BULK_ANNOUNCE:
            (self.total,) = struct.unpack_from("<I", payload, 1)
            self.next_expected = self.acked = 0
            self._ack(0)
        elif op == BULK_CHUNK:
            (offset,) = struct.unpack_from("<I", payload, 1)
            data = payload[5:]
            if offset != self.next_expected:
                self._ack(self.next_expected)  # §2.7 rule 5: offset-keyed, no write
                return
            self.next_expected += len(data)
            self.writes[offset] += 1
            self.bytes_written += len(data)
            if (self.next_expected - self.acked >= self.window
                    or self.next_expected == self.total):
                self.acked = self.next_expected
                self._ack(self.acked)
        elif op == BULK_COMMIT:
            (total,) = struct.unpack_from("<I", payload, 1)
            assert total == self.total and self.next_expected == self.total
            self.commits += 1
            # The instrument prints as staging closes; the commit reply follows.
            if self.emit_ring_line:
                self.noise_log.append(
                    f"I (12345) control: staging closed: total={self.total}B "
                    f"ring hw={self.ring_hw}/{self.ring_capacity} "
                    f"spins={self.ring_spins} "
                    f"backlog={self.backlog_hw}/{self.backlog_capacity} "
                    f"drops={self.ring_drops}\r\n".encode())
            self.status_plan_hash = payload[5:21]
            self.status_frame_count = (self.total - 12) // 11
            self._ack(self.total)

    def wait_reply(self, verb: int, timeout: float):
        if not self.replies:
            raise TimeoutError("no reply")  # the stall the uploader must ride out
        frame_verb, payload = self.replies.popleft()
        assert frame_verb == verb, f"reply for verb {frame_verb}, waiting for {verb}"
        return _Frame(frame_verb, payload)

    # ── internals ──────────────────────────────────────────────────────────

    def _ack(self, offset: int) -> None:
        self.replies.append((VERB_LOAD_PLAN, struct.pack("<I", offset)))

    def _status_payload(self) -> bytes:
        p = bytearray(47)
        p[2] = MODE_IDLE
        p[3] = PLAN_COMMITTED if self.commits else 0
        if self.force_status_hash is not None:
            p[4:20] = self.force_status_hash
        elif self.status_plan_hash is not None:
            p[4:20] = self.status_plan_hash
        struct.pack_into("<H", p, 20, self.status_frame_count)
        return bytes(p)


class StallingDevice(RingDevice):
    """A saturated ring: one mid-window chunk is lost to the blast.

    The dropped chunk fails the device's CRC silently (§2.8), the chunks
    behind it draw only the offset-mismatch ACK, the uploader's window stalls
    once and resends — and the meter reads the ring full: the 921600
    positive-control shape, one `window_retry` and a window's bytes resent.
    """

    def __init__(self, drop_offset: int):
        super().__init__()
        self.drop_offset = drop_offset
        self.dropped = False
        self.ring_hw = 255
        self.ring_spins = 40000
        self.backlog_hw = 256   # the driver ring backed up to its cap: the cliff
        self.ring_drops = 1     # the corrupted chunk was silently untrusted

    def send(self, verb: int, payload: bytes) -> None:
        if (verb == VERB_LOAD_PLAN and payload[0] == BULK_CHUNK and not self.dropped
                and struct.unpack_from("<I", payload, 1)[0] == self.drop_offset):
            self.dropped = True  # lost once; the resend arrives intact
            return
        super().send(verb, payload)


# ── the ring-line contract ────────────────────────────────────────────────────


class RingLine(unittest.TestCase):
    def test_parse_accepts_the_device_format(self):
        line = ("I (618932) control: staging closed: total=65528B "
                "ring hw=255/256 spins=13699 backlog=0/256 drops=0\r\n")
        self.assertEqual(parse_ring_line(line),
                         dict(total=65528, hw=255, capacity=256, spins=13699,
                              backlog=0, backlog_capacity=256, drops=0))

    def test_parse_rejects_everything_else(self):
        for line in (
            "I (1) control: transport_layer: UART opened",
            "I (2) control: staging closed: total=65528B ring hw=255/256",  # truncated
            "staging closed: total=65528B ring hw=255/256 spins=1 backlog=0/256 drops=0",  # no skin
            "D (3) control: staging closed: total=65528B ring hw=255/256 spins=1 "
            "backlog=0/256 drops=0",  # DEBUG: compiled out on the deployment build
            "",
        ):
            self.assertIsNone(parse_ring_line(line), repr(line))

    def test_scan_extracts_lines_in_order_from_noise_bytes(self):
        noise = (b"I (1) transport_layer: UART transport initialized\r\n"
                 b"I (2) control: staging closed: total=793B ring hw=192/256 "
                 b"spins=2100 backlog=17/256 drops=0\r\n"
                 b"I (3) hid: skipping report send\r\n"
                 b"I (4) control: staging closed: total=3356B ring hw=255/256 "
                 b"spins=9900 backlog=41/256 drops=2\r\n")
        self.assertEqual(scan_ring_lines(noise),
                         [dict(total=793, hw=192, capacity=256, spins=2100,
                               backlog=17, backlog_capacity=256, drops=0),
                          dict(total=3356, hw=255, capacity=256, spins=9900,
                               backlog=41, backlog_capacity=256, drops=2)])
    def test_scan_survives_a_segment_glued_by_a_truncated_boot_tail(self):
        # A real capture's first transfer: the boot banner's last segment was
        # cut mid-line by a frame delimiter, so it has no trailing newline —
        # concatenating segments would glue it onto the instrument line and
        # hide it (found on the bench, on exactly this run's first transfer).
        noise = (b"I (25) boot: chip revision: v0.2"      # no trailing newline
                 b"I (1428) control: staging closed: total=793B ring hw=217/256 "
                 b"spins=1433 backlog=114/256 drops=0\n")
        self.assertEqual(scan_ring_lines(noise),
                         [dict(total=793, hw=217, capacity=256, spins=1433,
                               backlog=114, backlog_capacity=256, drops=0)])


# ── per-transfer rows ─────────────────────────────────────────────────────────


class RunTransfer(unittest.TestCase):
    def setUp(self):
        self.payload, self.identity = synthesize_plan(371)  # 4,093 B: window + 1

    def test_a_clean_transfer_carries_the_meter_reading(self):
        device = RingDevice()
        row = run_transfer(device, label="synthetic-4093",
                           payload=self.payload, identity=self.identity,
                           request_timeout=0.05, ack_timeout=0.05)
        self.assertEqual(row["bytes"], 4093)
        self.assertTrue(row["status_ok"])
        self.assertEqual(row["window_retries"], 0)
        self.assertEqual(row["ring_hw"], 255)      # steady state under a blast
        self.assertEqual(row["ring_drops"], 0)     # nothing untrusted
        self.assertEqual(row["backlog_hw"], 41)    # the margin number
        self.assertEqual(row["ring_capacity"], 256)
        self.assertTrue(row["ring_line_found"])
        self.assertEqual(device.commits, 1)

    def test_a_missing_ring_line_is_marked_not_guessed(self):
        device = RingDevice()
        device.emit_ring_line = False
        row = run_transfer(device, label="silent-meter",
                           payload=self.payload, identity=self.identity,
                           request_timeout=0.05, ack_timeout=0.05)
        self.assertFalse(row["ring_line_found"])
        self.assertIsNone(row["ring_hw"])
        self.assertIsNone(row["ring_drops"])

    def test_a_status_hash_mismatch_fails_the_row(self):
        device = RingDevice()
        device.force_status_hash = bytes(16)  # claims a different plan
        row = run_transfer(device, label="bad-echo",
                           payload=self.payload, identity=self.identity,
                           request_timeout=0.05, ack_timeout=0.05)
        self.assertFalse(row["status_ok"])

    def test_a_stall_and_a_full_ring_are_recorded_not_raised(self):
        device = StallingDevice(drop_offset=2048)
        row = run_transfer(device, label="control-4093",
                           payload=self.payload, identity=self.identity,
                           request_timeout=0.05, ack_timeout=0.05)
        self.assertEqual(row["window_retries"], 1)
        self.assertEqual(row["ring_drops"], 1)     # the corrupted chunk, seen
        self.assertEqual(row["backlog_hw"], 256)   # the driver ring at its cap
        self.assertEqual(row["ring_hw"], 255)
        self.assertTrue(row["status_ok"])  # the resend completed the transfer
        self.assertGreater(row["dup_acks"], 0)  # the mismatch ACKs behind the loss
        self.assertEqual(device.bytes_written, len(self.payload))  # mismatch writes nothing


# ── the verdict ───────────────────────────────────────────────────────────────


def _row(label, nbytes, *, retries=0, drops=0, hw=255, backlog=41, ok=True, line=True):
    return dict(label=label, bytes=nbytes, records=(nbytes - 12) // 11,
                status_ok=ok, window_retries=retries, ring_drops=drops,
                ring_hw=hw if line else None, ring_spins=9000 if line else None,
                backlog_hw=backlog if line else None,
                backlog_capacity=256 if line else None,
                ring_capacity=256 if line else None,
                ring_line_found=line, rx_bad_crc=0)


class VerdictClean(unittest.TestCase):
    def test_holds_and_reports_the_largest_clean_transfer(self):
        rows = [_row("real-杏仁", 3356), _row("synthetic-65528", 65528),
                _row("synthetic-540", 540)]
        v = verdict(rows, expect="clean")
        self.assertTrue(v["ring_holds"])
        self.assertEqual(v["ring_bytes"], 256)
        self.assertEqual(v["ack_window"], 4096)
        self.assertEqual(v["stall_count"], 0)
        self.assertEqual(v["drops"], 0)
        self.assertEqual(v["backlog_hw"], 41)
        self.assertEqual(v["max_hw"], 255)
        self.assertEqual(v["largest_clean"], 65528)
        self.assertEqual(v["failures"], [])

    def test_one_stall_fails_the_design_point(self):
        rows = [_row("synthetic-4093", 4093, retries=1, drops=1)]
        v = verdict(rows, expect="clean")
        self.assertFalse(v["ring_holds"])
        self.assertEqual(v["stall_count"], 1)
        self.assertTrue(v["failures"])

    def test_untrusted_frames_fail_even_without_a_stall(self):
        # The windowed ACK can absorb a lost chunk by luck of timing; frames the
        # decoder could not trust are the fact the ticket is after.
        rows = [_row("synthetic-65528", 65528, drops=2)]
        v = verdict(rows, expect="clean")
        self.assertFalse(v["ring_holds"])
        self.assertTrue(any("could not trust" in f for f in v["failures"]))

    def test_a_backlog_at_capacity_is_the_cliff_even_if_survived(self):
        rows = [_row("synthetic-65528", 65528, backlog=256)]
        v = verdict(rows, expect="clean")
        self.assertFalse(v["ring_holds"])
        self.assertTrue(any("cliff" in f for f in v["failures"]))

    def test_a_missing_line_or_bad_status_is_a_failure(self):
        for bad in (_row("silent", 540, line=False), _row("no-echo", 540, ok=False)):
            v = verdict([bad], expect="clean")
            self.assertFalse(v["ring_holds"], bad["label"])
            self.assertTrue(v["failures"], bad["label"])

    def test_disagreeing_ring_sizes_are_a_failure(self):
        rows = [_row("a", 540), dict(_row("b", 4093), ring_capacity=512)]
        v = verdict(rows, expect="clean")
        self.assertFalse(v["ring_holds"])
        self.assertTrue(any("ring size" in f for f in v["failures"]))


class VerdictDegenerate(unittest.TestCase):
    def test_the_positive_control_passes_when_the_meter_saw_loss(self):
        rows = [_row("control-4093", 4093, retries=13, drops=9, backlog=256)]
        v = verdict(rows, expect="degenerate")
        self.assertFalse(v["ring_holds"])
        self.assertEqual(v["stall_count"], 13)
        self.assertEqual(v["drops"], 9)
        self.assertEqual(v["failures"], [])

    def test_the_control_fails_when_the_meter_read_nothing(self):
        # A saturated line with a meter that sees no loss proves nothing.
        rows = [_row("control-4093", 4093, retries=13, drops=0)]
        v = verdict(rows, expect="degenerate")
        self.assertTrue(any("cannot certify" in f for f in v["failures"]))


# ── the real library ──────────────────────────────────────────────────────────


class LoadRealPlans(unittest.TestCase):
    def test_compiles_every_macro_and_sorts_by_plan_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            macro_dir = Path(tmp)
            # One real macro, copied from the golden fixture's source: the
            # loader must produce §5.7's row for it (793 B, 71 records).
            src = Path(__file__).resolve().parent.parent / "fixtures/plan/correction.json"
            (macro_dir / "纠错宏.json").write_bytes(src.read_bytes())
            plans = load_real_plans(macro_dir)
        self.assertEqual(len(plans), 1)
        name, payload, identity, records = plans[0]
        self.assertEqual(name, "纠错宏")
        self.assertEqual((len(payload), records), (793, 71))
        self.assertEqual(identity.hex()[:16], "1f0a90d3ccabcb19")

    def test_an_absent_mount_returns_empty_rather_than_raising(self):
        self.assertEqual(load_real_plans(Path("/nonexistent-macro-mount")), [])

    def test_a_macro_that_rejects_is_skipped_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            macro_dir = Path(tmp)
            (macro_dir / "bad.json").write_text(json.dumps([{"t": 0, "ev": {}}]))
            (macro_dir / "good.json").write_text(
                json.dumps([{"t": 0, "ev": {"type": "button", "name": "a", "pressed": True}},
                            {"t": 100, "ev": {"type": "button", "name": "a", "pressed": False}}]))
            plans = load_real_plans(macro_dir)
        self.assertEqual([p[0] for p in plans], ["good"])


if __name__ == "__main__":
    unittest.main()
