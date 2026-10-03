#!/usr/bin/env python3
"""Tests for `bench_link_drop.py` — the #38 mid-press / reboot-rate instrument.

Run:  python3 scripts/test_bench_link_drop.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only (the repo's rule for `scripts/` tests). Three things are
pinned, all without a board:

- **the fixture is what half A assumes.** `MID_PRESS_JSON` must compile to
  exactly the shape the drop condition needs — frame 0 a button press held 60 s,
  `loop_ms = 60000` — so a compiler change cannot quietly make "mid-press" a
  claim about a macro that no longer holds anything.
- **the meter parser.** The device's readout line formats (`macro meter: …`) are
  the raw material the verdict is computed from; the tests use the real
  firmware strings, including the `n=1` neutral flag.
- **the verdict.** Half A fails on a missing readout, a drop that did not land
  on the press record, a run with no non-neutral input, or a negative
  applied/notified gap; half B's tallies are read out of the same log stream the
  Logs screen renders, so a reboot must be counted from the container's own
  line and an overflow from the device's panic text.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench_link_drop import (
    MID_PRESS_JSON,
    NEUTRAL,
    LogTally,
    Meter,
    mid_press_failures,
    parse_meter_input,
    parse_meter_summary,
)

from container.ns2plan import compile_macro, iter_frames

SUMMARY = (
    "control: macro meter: applied=5 changes=4 inputs=3 notified=9 dropped=0 "
    "failed=0 ring=3/64 elapsed=100us"
)
NEUTRAL_LINE = "control: macro meter: #0 d=0us g=1 n=1 s=000000000880000880"
PRESS_LINE = "control: macro meter: #1 d=10000us g=2 s=020000000880000880"


class Fixture(unittest.TestCase):
    def test_the_neutral_is_the_spec_template(self):
        self.assertEqual(NEUTRAL.hex(), "000000000880000880")
        self.assertEqual(NEUTRAL[:3], bytes(3))  # buttons clear
        self.assertEqual(NEUTRAL[3], 0x00)
        self.assertEqual(NEUTRAL[4], 0x08)  # left stick centred
        self.assertEqual(NEUTRAL[5], 0x80)

    def test_the_mid_press_macro_compiles_to_a_held_press(self):
        plan = compile_macro(MID_PRESS_JSON)
        frames = list(iter_frames(plan))
        self.assertEqual(plan.loop_ms, 60000)
        self.assertEqual(len(frames), 2)
        (b0, _b1, _b2, *_sticks), first_hold = frames[0]
        self.assertEqual(first_hold, 60000)
        self.assertNotEqual(b0, 0, "frame 0 must carry a press")
        self.assertEqual(frames[1][0][:3], (0, 0, 0), "frame 1 is the neutral")


class MeterParsing(unittest.TestCase):
    def test_an_input_line_carries_its_state_and_neutral_flag(self):
        neutral = parse_meter_input(NEUTRAL_LINE)
        self.assertEqual(
            neutral,
            {"index": 0, "deltaUs": 0, "gen": 1, "neutral": True, "state": "000000000880000880"},
        )
        press = parse_meter_input(PRESS_LINE)
        self.assertEqual(press["neutral"], False)
        self.assertEqual(press["state"], "020000000880000880")

    def test_the_summary_line_carries_the_counters(self):
        self.assertEqual(
            parse_meter_summary(SUMMARY),
            {
                "applied": 5,
                "changes": 4,
                "inputs": 3,
                "notified": 9,
                "dropped": 0,
                "failed": 0,
                "ring": 3,
                "capacity": 64,
            },
        )

    def test_any_other_line_is_none(self):
        self.assertIsNone(parse_meter_input("ble_gap: disconnected, reason=531"))
        self.assertIsNone(parse_meter_summary("control: macro meter: #0 d=0us g=1 s=00"))
        self.assertIsNone(parse_meter_input(SUMMARY))


class MeterVerdict(unittest.TestCase):
    def _meter(self, *lines: str) -> Meter:
        meter = Meter()
        for line in lines:
            meter.feed(line)
        return meter

    def test_a_mid_press_stop_passes(self):
        # applied one more transition than notified: the stop's neutral write.
        meter = self._meter(SUMMARY, NEUTRAL_LINE, PRESS_LINE)
        self.assertTrue(meter.carried_a_press)
        self.assertEqual(meter.neutral_pending, 1)
        self.assertEqual(mid_press_failures(meter, 0), [])

    def test_a_missing_readout_is_a_failure(self):
        self.assertTrue(any("no meter readout" in f for f in mid_press_failures(Meter(), 0)))

    def test_a_drop_off_the_press_record_is_a_failure(self):
        meter = self._meter(SUMMARY, NEUTRAL_LINE, PRESS_LINE)
        self.assertTrue(any("press record" in f for f in mid_press_failures(meter, 1)))
        self.assertTrue(any("press record" in f for f in mid_press_failures(meter, None)))

    def test_a_run_with_no_non_neutral_input_is_a_failure(self):
        meter = self._meter(SUMMARY, NEUTRAL_LINE)
        self.assertTrue(any("not mid-press" in f for f in mid_press_failures(meter, 0)))

    def test_a_negative_gap_is_a_failure(self):
        # `inputs` can never exceed `changes`; if it does, the parser met a
        # readout it does not understand and the verdict must say so.
        bogus = SUMMARY.replace("changes=4", "changes=1")
        meter = self._meter(bogus, NEUTRAL_LINE, PRESS_LINE)
        self.assertTrue(any("not accounted for" in f for f in mid_press_failures(meter, 0)))


class Tally(unittest.TestCase):
    def test_counts_drops_reconnects_reboots_and_the_overflow(self):
        tally = LogTally()
        tally.feed("warn", "container", "console link: disconnected (reason=531 (BLE_HS_ERR_HCI_BASE + 0x13))")
        tally.feed("info", "container", "console link: connected")
        tally.feed("info", "container", "console link: resubscribed")
        tally.feed("warn", "container", "recovery: the device reported a boot (boot_id=3bcc624c)")
        tally.feed("info", "device", "***ERROR*** A stack overflow in task Tmr Svc has been detected.")
        tally.feed("info", "device", "rst:0xc (RTC_SW_CPU_RST)")
        self.assertEqual((tally.drops, tally.reconnects), (1, 2))
        self.assertEqual((tally.boots, tally.overflows, tally.rst0xc), (1, 1, 1))
        self.assertEqual(tally.reason531, 1)

    def test_a_drop_the_console_did_not_end_is_not_counted_as_531(self):
        tally = LogTally()
        tally.feed("warn", "container", "console link: disconnected (reason=8 (connection timeout))")
        self.assertEqual((tally.drops, tally.reason531), (1, 0))

    def test_a_clean_cycle_session_counts_nothing(self):
        tally = LogTally()
        tally.feed("info", "container", "console link: connected")
        self.assertEqual(
            (tally.drops, tally.reason531, tally.boots, tally.overflows, tally.rst0xc),
            (0, 0, 0, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
