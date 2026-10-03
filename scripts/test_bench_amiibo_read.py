#!/usr/bin/env python3
"""Tests for `bench_amiibo_read.py` — the #36 polling/read instrument.

Run:  python3 scripts/test_bench_amiibo_read.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only (the repo's rule for `scripts/` tests). Pinned, all
without a board:

- **the trace parser.** The device's `console nfc:` line formats (issue #36's
  firmware half) are the raw material every verdict is computed from; the tests
  feed the exact strings `nfc_trace.c` emits, including the `0x0C` probe line
  `ns2_codec.c` logs inline.
- **the CRC.** §2.2's call (CRC-16/CCITT-FALSE, init `0xFFFF`) must agree with
  the firmware's, or "every byte arrived uncorrupted" would be self-referential.
- **the read analysis.** `image_slice_for`/`wire_coverage`/`verify_reads`
  together answer validation 5 under both §6.6 candidate offset spaces: the
  plain one (wire == image) and the capture's shift (wire == image + 0x3C),
  including the case where only one of them can be what the console meant.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_amiibo_read import (
    WIRE_BASE_CAPTURE,
    WIRE_BASE_PLAIN,
    NfcTally,
    crc16_ccitt,
    image_slice_for,
    parse_nfc_line,
    read_span_ms,
    round_trips,
    verify_reads,
    wire_coverage,
)


def make_read(off_wire: int, image: bytes, base: int, t: int = 0, chunk: int = 64) -> dict:
    """A traced `0x15` event exactly as the device would emit it for `image`."""
    chunk = min(chunk, len(image) - (off_wire - base))
    payload = image_slice_for(image, off_wire, 3 + chunk, base)
    assert payload is not None and len(payload) == 3 + chunk
    return {"kind": "event", "t": t, "sub": 0x15, "off": off_wire, "n": 3 + chunk,
            "crc": crc16_ccitt(payload)}


class Crc(unittest.TestCase):
    def test_it_is_ccitt_false(self):
        # The reference vector for CRC-16/CCITT-FALSE.
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_it_matches_the_firmware_call(self):
        # control_crc16_update(0xffff, …): poly 0x1021, MSB-first, no reflection
        # — one message and its CRC computed by hand the same way.
        msg = bytes(range(67))
        crc = 0xFFFF
        for byte in msg:
            crc ^= byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
                crc &= 0xFFFF
        self.assertEqual(crc16_ccitt(msg), crc)


class Parser(unittest.TestCase):
    def test_a_read_line_parses(self):
        event = parse_nfc_line("console nfc: t=4331 sub=15 off=0046 n=67 crc=e5f6 reps=1")
        self.assertIsNotNone(event)
        self.assertEqual(event["kind"], "event")
        self.assertEqual(event["t"], 4331)
        self.assertEqual(event["sub"], 0x15)
        self.assertEqual(event["off"], 0x46)
        self.assertEqual(event["n"], 67)
        self.assertEqual(event["crc"], 0xE5F6)
        self.assertEqual(event["reps"], 1)

    def test_a_status_line_parses_with_hex_fields(self):
        event = parse_nfc_line("console nfc: t=4320 sub=05 status=09 n=61 crc=041c reps=12")
        self.assertEqual(event["status"], 0x09)
        self.assertEqual(event["n"], 61)
        self.assertEqual(event["crc"], 0x041C)

    def test_a_write_line_carries_its_bytes(self):
        event = parse_nfc_line(
            "console nfc: t=4330 sub=14 off=0000 want=76 got=4 crc=c3d4 data=d007048a")
        self.assertEqual(event["sub"], 0x14)
        self.assertEqual(event["off"], 0)
        self.assertEqual(event["want"], 76)
        self.assertEqual(event["data"], bytes.fromhex("d007048a"))

    def test_a_poll_line_carries_its_configuration(self):
        event = parse_nfc_line("console nfc: t=4312 sub=03 len=5 cfg=00e8032c01")
        self.assertEqual(event["sub"], 0x03)
        self.assertEqual(event["cfg"], bytes.fromhex("00e8032c01"))

    def test_the_summary_line_parses(self):
        summary = parse_nfc_line(
            "console nfc: scan cmds=15 [03=1 04=1 05=13 06=1 14=1 15=9] reps=12 drops=0")
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["cmds"], 15)
        self.assertEqual(summary["c15"], 9)
        self.assertEqual(summary["drops"], 0)

    def test_the_probe_line_parses(self):
        probe = parse_nfc_line("console nfc: sub=0c rsp=61125010")
        self.assertEqual(probe, {"kind": "probe", "rsp": "61125010"})

    def test_any_other_line_is_none(self):
        self.assertIsNone(parse_nfc_line("macro meter: applied=5 changes=5"))
        self.assertIsNone(parse_nfc_line("console link: connected"))

    def test_the_containers_tag_prefix_is_tolerated(self):
        # The container's log demux keeps the ESP-IDF tag (§8.2's log_lines): the
        # first run of this bench lost every line to an anchored match.
        event = parse_nfc_line("control: console nfc: t=6867 sub=05 status=09 n=61 crc=eb95 reps=3")
        self.assertEqual(event["sub"], 0x05)
        self.assertEqual(event["status"], 0x09)
        self.assertEqual(event["reps"], 3)
        summary = parse_nfc_line(
            "control: console nfc: scan cmds=4 [03=0 04=0 05=4 06=0 14=0 15=0] reps=3 drops=0")
        self.assertEqual(summary["kind"], "summary")


class Tally(unittest.TestCase):
    def test_probes_and_events_are_kept_apart(self):
        tally = NfcTally()
        tally.feed("console nfc: sub=0c rsp=61125010")
        tally.feed("console nfc: t=10 sub=03 len=5 cfg=00e8032c01")
        tally.feed("console nfc: t=11 sub=15 off=0046 n=67 crc=e5f6 reps=1")
        self.assertEqual(tally.probes, ["61125010"])
        self.assertEqual(len(tally.events), 2)
        self.assertTrue(tally.polled)

    def test_probe_only_is_not_polled(self):
        tally = NfcTally()
        tally.feed("console nfc: sub=0c rsp=61125010")
        self.assertFalse(tally.polled)

    def test_reads_and_writes_slice_by_index(self):
        tally = NfcTally()
        tally.feed("console nfc: t=1 sub=15 off=0046 n=67 crc=e5f6 reps=1")
        tally.feed("console nfc: t=2 sub=14 off=0000 want=4 got=4 crc=c3d4 data=d0")
        tally.feed("console nfc: t=3 sub=15 off=0086 n=67 crc=e5f6 reps=1")
        self.assertEqual([r["off"] for r in tally.reads()], [0x46, 0x86])
        self.assertEqual([r["off"] for r in tally.reads(1)], [0x86])
        self.assertEqual([w["off"] for w in tally.writes(1)], [0])


class ReadAnalysis(unittest.TestCase):
    IMAGE = bytes((i * 7 + 3) & 0xFF for i in range(540))

    def test_a_full_read_under_the_plain_space(self):
        reads = [make_read(off, self.IMAGE, WIRE_BASE_PLAIN, t=1000 * i)
                 for i, off in enumerate(range(0, 540, 64))]
        self.assertEqual(round_trips(reads), 9)
        self.assertEqual(read_span_ms(reads), 8000)
        coverage = wire_coverage(reads, WIRE_BASE_PLAIN)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["imageBytesCovered"], 540)
        verdict = verify_reads(reads, self.IMAGE, base=WIRE_BASE_PLAIN)
        self.assertEqual((verdict["checked"], verdict["mismatched"]), (9, 0))
    def test_the_same_reads_are_not_the_capture_space(self):
        reads = [make_read(off, self.IMAGE, WIRE_BASE_PLAIN)
                 for off in range(0, 540, 64)]
        # Under the capture's shift these wire offsets would have served image
        # bytes 0x3C lower than what actually went out, so the CRCs cannot hold.
        verdict = verify_reads(reads, self.IMAGE, base=WIRE_BASE_CAPTURE)
        self.assertEqual(verdict["checked"], 0)

    def test_the_capture_space_reproduces_the_canonical_exchange(self):
        # The one observed console exchange: wire 0x46 for the image's 0x0A.
        payload = image_slice_for(self.IMAGE, 0x46, 67, WIRE_BASE_CAPTURE)
        self.assertEqual(payload[:3], b"\x00\x46\x00")
        self.assertEqual(payload[3:9], self.IMAGE[0x0A:0x10])
        read = {"kind": "event", "t": 0, "sub": 0x15, "off": 0x46, "n": 67,
                "crc": crc16_ccitt(payload)}
        verdict = verify_reads([read], self.IMAGE, base=WIRE_BASE_CAPTURE)
        self.assertEqual((verdict["checked"], verdict["mismatched"]), (1, 0))
        # And under the plain hypothesis the very same exchange fails — which is
        # exactly how the bench tells the two spaces apart.
        plain = verify_reads([read], self.IMAGE, base=WIRE_BASE_PLAIN)
        self.assertEqual(plain["checked"], 0)

    def test_the_capture_space_reads_cover_the_image(self):
        # Nine reads at wire 0x46 + 0x40k: the last one clips to the image's end.
        offs = [0x46 + 0x40 * k for k in range(9)]
        reads = []
        for off in offs:
            chunk = min(64, 540 - (off - WIRE_BASE_CAPTURE))
            payload = image_slice_for(self.IMAGE, off, 3 + chunk, WIRE_BASE_CAPTURE)
            reads.append({"kind": "event", "t": 0, "sub": 0x15, "off": off,
                          "n": 3 + chunk, "crc": crc16_ccitt(payload)})
        coverage = wire_coverage(reads, WIRE_BASE_CAPTURE)
        # Nine reads from wire 0x46 cover image 0x0A..0x21B — 530 bytes. The
        # missing ten are the UID block *below* the capture base: under this
        # hypothesis `0x05` supplies them, and `0x15` never can. So `complete`
        # (all 540) is honestly false, and `completeFromFirst` — everything from
        # the first read's own address up — is the honest "it read everything it
        # could have".
        self.assertEqual(coverage["imageBytesCovered"], 530)
        self.assertFalse(coverage["complete"])
        self.assertTrue(coverage["completeFromFirst"])
        self.assertEqual(coverage["firstImageOffset"], 0x0A)
        verdict = verify_reads(reads, self.IMAGE, base=WIRE_BASE_CAPTURE)
        self.assertEqual(verdict["mismatched"], 0)
        self.assertEqual(verdict["checked"], 9)

    def test_a_gap_in_the_middle_is_not_complete_from_first(self):
        reads = [make_read(off, self.IMAGE, WIRE_BASE_PLAIN) for off in (0, 128, 256)]
        coverage = wire_coverage(reads, WIRE_BASE_PLAIN)
        self.assertFalse(coverage["complete"])
        self.assertFalse(coverage["completeFromFirst"])
        self.assertEqual(coverage["imageBytesCovered"], 192)

    def test_a_partial_read_is_not_complete(self):
        reads = [make_read(0, self.IMAGE, WIRE_BASE_PLAIN)]
        coverage = wire_coverage(reads, WIRE_BASE_PLAIN)
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["imageBytesCovered"], 64)

    def test_a_corrupted_serving_is_mismatched(self):
        read = make_read(0, self.IMAGE, WIRE_BASE_PLAIN)
        read["crc"] ^= 1
        verdict = verify_reads([read], self.IMAGE, base=WIRE_BASE_PLAIN)
        self.assertEqual((verdict["checked"], verdict["mismatched"]), (0, 1))

    def test_a_write_is_applied_before_later_reads_verify(self):
        write = {"kind": "event", "t": 0, "sub": 0x14, "off": 0, "want": 4, "got": 4,
                 "crc": 0, "data": b"\xde\xad\xbe\xef"}
        # The served response after the write: the console's own bytes, then the
        # image's remainder — reconstructed by hand so the CRC is independent of
        # the code under test.
        served = bytes(write["data"]) + self.IMAGE[4:64]
        read = {"kind": "event", "t": 1, "sub": 0x15, "off": 0, "n": 67,
                "crc": crc16_ccitt(b"\x00\x00\x00" + served)}
        verdict = verify_reads([read], self.IMAGE, [write], base=WIRE_BASE_PLAIN)
        self.assertEqual((verdict["checked"], verdict["mismatched"]), (1, 0))
        # Without the write applied, the same exchange is a mismatch — proving
        # the check above is the write's doing and not a tautology.
        self.assertEqual(verify_reads([read], self.IMAGE, base=WIRE_BASE_PLAIN)["checked"], 0)

    def test_a_read_before_the_write_sees_the_pre_write_bytes(self):
        # The console reads the head first, then writes it back: the read's CRC
        # must be checked against the image as it was *before* the write —
        # applying writes upfront would mismatmatch an honest read.
        write = {"kind": "event", "t": 10, "sub": 0x14, "off": 0, "want": 4, "got": 4,
                 "crc": 0, "data": b"\xde\xad\xbe\xef"}
        read = make_read(0, self.IMAGE, WIRE_BASE_PLAIN, t=5)
        verdict = verify_reads([read], self.IMAGE, [write], base=WIRE_BASE_PLAIN)
        self.assertEqual((verdict["checked"], verdict["mismatched"]), (1, 0))

    def test_an_unaddressable_read_is_counted_not_failed(self):
        # Below the capture base there is no image byte to reconstruct.
        read = {"kind": "event", "t": 0, "sub": 0x15, "off": 0x10, "n": 67, "crc": 0}
        verdict = verify_reads([read], self.IMAGE, base=WIRE_BASE_CAPTURE)
        self.assertEqual(verdict["unaddressable"], 1)


if __name__ == "__main__":
    unittest.main()
