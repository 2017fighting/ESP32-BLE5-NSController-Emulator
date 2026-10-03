#!/usr/bin/env python3
"""Tests for `bench_macro_timing.py` — the #35 report-cadence measurement.

Run:  python3 scripts/test_bench_macro_timing.py
      python3 -m unittest discover -s scripts -p 'test_*.py'

Stdlib `unittest` only (the repo's rule for `scripts/` tests). Four things are
pinned, all against fakes rather than the firmware:

- **the meter-line contract.** The instrumented device prints one block per run
  at `INFO` when the mode exits — a summary line, a percentile line, a handoff
  line and one line per distinct input. The block arrives *inside the log noise
  the link already carries*, split across however many frame delimiters happened
  to fall, so the parse must survive a banner in front of it and a block cut
  mid-segment.
- **the delivery metric.** `inputs / changes` is the collapse number: what the
  executor wrote against what the console was notified. The bench's first
  attempt matched notified bytes back to plan records instead, which cannot work
  — a plan may revisit a state (the real 纠错宏 has 47 distinct states across 71
  records), and a 5 ms macro aliases into a stream that still looks like a walk
  through the plan while 95% of its inputs are lost.
- **the plan read.** §5.4's last-record hold runs to `loop_ms`, so the effective
  holds are what a comparison against the measured deltas must use.
- **a silent instrument is a finding, not a zero.** #34's lesson: a case whose
  meter block never arrived must fail the bench rather than report no latency.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_macro_timing import (  # noqa: E402
    console_interval_ms,
    delivery,
    hold_stats,
    parse_meter,
    plan_records,
    report_case,
    sample_stats,
    synthetic,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "plan" / "correction.json"

# A captured block, verbatim in shape: the summary, the percentiles, the handoff
# and three inputs whose deltas are 0, 30 ms and 10 ms, the second of them the
# loop boundary's neutral.
BLOCK = b"""\
I (17010) ble_gap:  conn_itvl=4 conn_latency=0 supervision_timeout=500 encrypted=0 authenticated=0 bonded=0
I (22355) control: macro meter: applied=137 changes=120 inputs=60 notified=350 dropped=0 failed=0 ring=60/64 elapsed=3500000us
I (22356) control: macro meter: delta n=59 min=1900us max=30000us
I (22356) control: macro meter: handoff n=13 min=9900us mean=10020us max=10600us
I (22356) control: macro meter: #0 d=0us g=1 s=020000000880000880
I (22360) control: macro meter: #1 d=30000us g=2 n=1 s=000000000880000880
I (22364) control: macro meter: #2 d=10000us g=3 s=010000000880000880
"""


class MeterParsing(unittest.TestCase):
    def test_a_block_is_parsed(self) -> None:
        meter = parse_meter([BLOCK])
        self.assertEqual(meter["applied"], 137)
        self.assertEqual(meter["changes"], 120)
        self.assertEqual(meter["inputs_total"], 60)
        self.assertEqual(meter["notified"], 350)
        self.assertEqual(meter["dropped"], 0)
        self.assertEqual(meter["elapsed_us"], 3_500_000)
        self.assertFalse(meter["wrapped"])
        self.assertEqual(meter["delta_n"], 59)
        self.assertEqual(meter["delta_min_us"], 1900)
        self.assertEqual(meter["delta_max_us"], 30_000)
        self.assertEqual(meter["handoffs"], 13)
        self.assertEqual(meter["handoff_min_us"], 9900)
        self.assertEqual(meter["handoff_mean_us"], 10_020)
        self.assertEqual(meter["handoff_max_us"], 10_600)
        self.assertEqual([entry["delta_us"] for entry in meter["inputs"]],
                         [0, 30_000, 10_000])
        self.assertEqual(meter["inputs"][2]["state"], "010000000880000880")
        self.assertEqual(meter["inputs"][1]["gen"], 2)
        self.assertTrue(meter["inputs"][1]["neutral"], "the boundary is flagged")
        self.assertFalse(meter["inputs"][0]["neutral"])

    def test_a_block_split_across_segments_is_parsed(self) -> None:
        """The read splits on frame delimiters, so the same block can arrive as
        several noise segments — including one cut mid-line."""
        segments = [BLOCK[:120], BLOCK[120:300], BLOCK[300:]]
        meter = parse_meter(segments)
        self.assertEqual(len(meter["inputs"]), 3)
        self.assertEqual(meter["notified"], 350)
        self.assertEqual(meter["handoffs"], 13)

    def test_a_wrapped_ring_is_reported(self) -> None:
        wrapped = BLOCK.replace(b"ring=60/64", b"ring=64/64 (wrapped)")
        self.assertTrue(parse_meter([wrapped])["wrapped"])

    def test_a_missing_block_is_no_block(self) -> None:
        """A device that never printed the meter line must not read as zeros."""
        meter = parse_meter([b"I (123) boot: nothing to see here\n"])
        self.assertIsNone(meter["notified"])
        self.assertEqual(meter["inputs"], [])


class SampleStats(unittest.TestCase):
    """The script's own distribution, because the meter keeps none on purpose."""

    def test_percentiles_come_from_the_sample(self) -> None:
        stats = sample_stats([0, 10_000, 20_000, 30_000, 40_000])
        self.assertEqual((stats["min"], stats["max"]), (0, 40_000))
        self.assertEqual(stats["p50"], 20_000)
        self.assertEqual(stats["p95"], 40_000)
        self.assertEqual(stats["n"], 5)

    def test_an_empty_sample_has_no_numbers(self) -> None:
        self.assertEqual(sample_stats([])["n"], 0)

    def test_the_console_interval_is_read_from_the_same_capture(self) -> None:
        """`conn_itvl` is in units of 1.25 ms, so 4 is the 5 ms link of §9.1."""
        line = b"I (17010) control: console link: conn_itvl=4 conn_itvl_ms=5\n"
        self.assertEqual(console_interval_ms([line]), 5.0)

    def test_a_capture_without_the_line_says_so(self) -> None:
        self.assertIsNone(console_interval_ms([b"nothing here\n"]))


class Delivery(unittest.TestCase):
    def test_the_ratio_is_inputs_over_changes(self) -> None:
        self.assertAlmostEqual(delivery(120, 60), 0.5)
        self.assertAlmostEqual(delivery(114, 114), 1.0)

    def test_a_five_millisecond_macro_reads_as_almost_total_loss(self) -> None:
        """The bench's grid-5ms case: the executor produced 1143 changes and the
        console was notified 56 of them."""
        self.assertLess(delivery(1143, 56), 0.06)

    def test_no_changes_is_no_ratio(self) -> None:
        self.assertEqual(delivery(0, 0), 0.0)


class PlanReading(unittest.TestCase):
    def test_the_golden_fixture_reads_back(self) -> None:
        from container.ns2plan import compile_json

        plan = compile_json(FIXTURE.read_text())
        records = plan_records(plan.payload)
        self.assertEqual(len(records), 71)
        self.assertEqual(records[0]["state"], "400000000880000880")
        self.assertEqual(records[0]["hold_ms"], 125)
        self.assertEqual(sum(record["hold_ms"] for record in records), 26_205,
                         "the effective holds sum to loop_ms (§5.4)")
        self.assertEqual(records[-1]["hold_ms"], 0,
                         "the last record's effective hold is what is left of loop_ms")
        self.assertEqual(len({record["state"] for record in records}), 47,
                         "the plan revisits states, which is why delivery is the "
                         "collapse metric and byte-matching is not")

    def test_a_synthetic_grid_has_the_spacing_it_was_asked_for(self) -> None:
        plan = synthetic(25.0, 20)
        records = plan_records(plan.payload)
        self.assertEqual(len(records), 20)
        self.assertEqual({record["hold_ms"] for record in records[:19]}, {25})
        self.assertEqual(plan.loop_ms, 475)

    def test_hold_stats_are_the_plan_s_own_distribution(self) -> None:
        stats = hold_stats(plan_records(synthetic(30.0, 10).payload))
        self.assertEqual((stats["min"], stats["p50"], stats["max"]), (0, 30, 30))
        self.assertEqual(stats["n"], 10)


def case_with(meter: dict, console_link: int = 1, console_interval_ms_: float | None = 5.0) -> dict:
    run = {"first": {"loop_count": 0}, "last": {"loop_count": 9, "console_link": console_link},
           "first_at": 100.0, "last_at": 105.0, "distinct_frames": [0, 1]}
    return {"name": "a case", "records": plan_records(synthetic(10.0, 2).payload),
            "plan": synthetic(10.0, 2), "run": run, "meter": meter,
            "console_link": console_link, "console_interval_ms": console_interval_ms_}


class SilentInstrument(unittest.TestCase):
    def test_a_case_without_a_meter_block_fails(self) -> None:
        meter = parse_meter([b"no meter here\n"])
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(meter), verbose=False)
        self.assertEqual(len(failures), 1)
        self.assertIn("no macro meter block", failures[0])

    def test_a_connected_console_with_no_notifications_fails(self) -> None:
        """G-16's question is answered by notifications: with the console
        CONNECTED and nothing notified, the measurement did not happen."""
        block = (b"macro meter: applied=10 changes=10 inputs=0 notified=0 dropped=0 "
                 b"failed=0 ring=0/64 elapsed=1000000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block])), verbose=False)
        self.assertEqual(len(failures), 1)
        self.assertIn("nothing was notified", failures[0])

    def test_the_no_console_case_is_not_a_failure(self) -> None:
        """G-4 is observed, not failed: an absent console notifies nothing."""
        block = (b"macro meter: applied=96 changes=96 inputs=0 notified=0 dropped=0 "
                 b"failed=0 ring=0/64 elapsed=1000000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block]), console_link=0),
                                   verbose=False, expect_console=False)
        self.assertEqual(failures, [])

    def test_a_console_that_arrives_during_a_no_console_run_fails(self) -> None:
        """The NS2 in standby reconnects to a bonded controller by itself (§9.1),
        which is how a --no-console window is lost. A case that ran with a console
        attached must never be quoted as the absent-console behaviour."""
        block = (b"macro meter: applied=96 changes=96 inputs=95 notified=500 dropped=0 "
                 b"failed=0 ring=64/64 elapsed=1000000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block]), console_link=1),
                                   verbose=False, expect_console=False)
        self.assertEqual(len(failures), 1)
        self.assertIn("connected during a --no-console run", failures[0])

    def test_a_capture_without_the_console_s_interval_fails(self) -> None:
        """Half of validation 3 is the comparison against the console's own link,
        so a run whose capture never carried `conn_itvl` did not make it."""
        block = (b"macro meter: applied=40 changes=38 inputs=35 notified=120 dropped=0 "
                 b"failed=0 ring=35/64 elapsed=1200000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block]), console_interval_ms_=None),
                                   verbose=False)
        self.assertEqual(len(failures), 1)
        self.assertIn("connection-interval", failures[0])

    def test_more_inputs_than_changes_is_an_inconsistency(self) -> None:
        block = (b"macro meter: applied=4 changes=3 inputs=5 notified=40 dropped=0 "
                 b"failed=0 ring=5/64 elapsed=1000000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block])), verbose=False)
        self.assertEqual(len(failures), 1)
        self.assertIn("inconsistent", failures[0])

    def test_a_truncated_block_still_reports_what_arrived(self) -> None:
        """Log text rides the same wire as everything else, so a line can be lost
        in a flooded window. What arrived is still the measurement; refusing to
        read it would throw away a run."""
        block = (b"macro meter: applied=40 changes=38 inputs=35 notified=120 dropped=0 "
                 b"failed=0 ring=35/64 elapsed=1200000us\n")
        with redirect_stdout(io.StringIO()):
            failures = report_case(case_with(parse_meter([block])), verbose=False)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
