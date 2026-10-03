#!/usr/bin/env python3
"""Measure the plan executor's input-to-input latency at the HID wire (issue #35,
§12.2 validation 3, gaps G-4 and G-16).

§12.2 asks whether the plan executor holds timing **against the console**, and
this is the only instrument that can answer it: the executor knows when a record
was *applied*, but the console sees notifications, and the report task is the
only place that knows when one left the radio. So the firmware carries a meter
(`macro_meter_*`, §7.5) that records every *distinct* notified state with the
microsecond clock, and prints the run at `INFO` when the mode exits — never per
report, because on a DEBUG build the report rate is set by the UART log budget
rather than by the report period, and a per-report log would be the measurement's
floor instead of its subject.

This script drives the run and reads the meter out of the same serial stream:

  * for each case it uploads a plan, `START`s it, polls `STATUS` while it runs,
    `STOP`s it, and drains the meter block the exit printed;
  * it pairs the measured inputs against the plan's own records (by their nine
    state bytes) and reports intended against measured hold;
  * it reports the records that never appeared at all — the inputs a report
    period shorter than the plan's records swallows.

The console side is a *real* NS2, connected and subscribed: no notification goes
out unless it is (`hid_controller.c` skips the swap), so `notified > 0` in the
meter line is itself the proof that a console was on the link. Run the same
script with the console absent and the same meter line reads `notified=0` — G-4's
observation, from the device's side.

Usage:
    python3 scripts/bench_macro_timing.py --port /dev/cu.usbmodemXXXX [--seconds 5]
    python3 scripts/bench_macro_timing.py --port ... --no-console   # G-4
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench_plan_executor import (  # noqa: E402
    ControlLink,
    MODE_IDLE,
    MODE_MACRO,
    VERB_HELLO,
    VERB_START,
    VERB_STOP,
    load_plan,
    parse_hello,
    status_of,
)
from container.ns2plan import compile_json, compile_macro, plan_identity  # noqa: E402

CONSOLE_LINK_NAMES = {0: "ADVERTISING", 1: "CONNECTED"}

HEADER_SIZE = 12
RECORD_SIZE = 11

# The meter's four line shapes (`control_meter.c`). Parsed out of the log noise
# the same serial stream carries, which is why the framing has to survive log
# text (§2.2) — this is the instrument riding that same tolerance.
RE_SUMMARY = re.compile(
    r"macro meter: applied=(\d+) changes=(\d+) inputs=(\d+) notified=(\d+) dropped=(\d+) "
    r"failed=(\d+) ring=(\d+)/(\d+)(?: \((wrapped)\))? elapsed=(\d+)us")
RE_DELTAS = re.compile(
    r"macro meter: delta n=(\d+) min=(\d+)us max=(\d+)us")
RE_CONN_ITVL = re.compile(r"conn_itvl=(\d+) conn_itvl_ms=(\d+)")
RE_HANDOFF = re.compile(
    r"macro meter: handoff n=(\d+) min=(\d+)us mean=(\d+)us max=(\d+)us")
RE_LOOP = re.compile(
    r"macro meter: loop n=(\d+) min=(\d+)us mean=(\d+)us max=(\d+)us")
RE_INPUT = re.compile(
    r"macro meter: #(\d+) d=(\d+)us g=(\d+)( n=1)? s=([0-9a-f]{18})")


# ------------------------------------------------------------------- the plan


def plan_records(payload: bytes) -> list[dict]:
    """The plan's records as the console would see them: nine state bytes and a
    hold (§5.3). The header is the `static_assert`ed layout, read directly.

    **The last record's hold is its effective one.** §5.4 makes the last record
    run to `loop_ms`, whatever its bytes say (the compiler writes a 0 there), so
    a comparison against the raw byte would call every loop boundary early.
    """
    count = int.from_bytes(payload[6:8], "little")
    loop_ms = int.from_bytes(payload[8:12], "little")
    records = []
    for i in range(count):
        r = payload[HEADER_SIZE + i * RECORD_SIZE : HEADER_SIZE + (i + 1) * RECORD_SIZE]
        records.append({"state": r[0:9].hex(),
                        "hold_ms": int.from_bytes(r[9:11], "little")})
    if records:
        spent = sum(record["hold_ms"] for record in records[:-1])
        records[-1]["hold_ms"] = max(0, loop_ms - spent)
    return records


def synthetic(grid_ms: float, count: int):
    """A macro whose records are exactly @p grid_ms apart: alternating A presses,
    each of which is a distinct state, so the plan's intent is unambiguous."""
    events = [
        {"t": i * grid_ms, "ev": {"type": "button", "name": "a", "pressed": i % 2 == 0}}
        for i in range(count)
    ]
    return compile_macro(events)


def cases(seconds: float, library: Path | None) -> list[tuple[str, object]]:
    """The windows that answer the ticket. `library` is a real macro at its own
    holds, which is the case ADR-0003 actually has to survive; the grids bracket
    the report period from either side so the quantisation is visible rather than
    inferred."""
    out: list[tuple[str, object]] = []
    if library is not None and library.exists():
        out.append((f"library {library.name}", compile_json(library.read_text())))
    out += [
        ("grid 50 ms", synthetic(50.0, 12)),
        ("grid 25 ms", synthetic(25.0, 20)),
        ("grid 10 ms", synthetic(10.0, 20)),
        ("grid 5 ms", synthetic(5.0, 20)),
        ("boundary", compile_macro([
            {"t": 0.0, "ev": {"type": "button", "name": "a", "pressed": True}},
            {"t": 120.0, "ev": {"type": "button", "name": "a", "pressed": False}},
            {"t": 240.0, "ev": {"type": "button", "name": "b", "pressed": True}},
            {"t": 360.0, "ev": {"type": "button", "name": "b", "pressed": False}},
        ])),
    ]
    return out


# ------------------------------------------------------------------ the meter


def parse_meter(noise: list[bytes]) -> dict:
    """The meter block, out of the log segments the read kept.

    A segment is whatever sat between two frame delimiters, so the block arrives
    as several segments and sometimes with a boot banner in front of it; matching
    line-wise is what makes that harmless.
    """
    text = b"".join(noise).decode("utf-8", "replace")
    meter: dict = {"inputs": [], "notified": None, "elapsed_us": None}
    for line in text.splitlines():
        if (m := RE_SUMMARY.search(line)) is not None:
            meter.update(applied=int(m[1]), changes=int(m[2]), inputs_total=int(m[3]),
                         notified=int(m[4]), dropped=int(m[5]), failed=int(m[6]),
                         ring=int(m[7]), ring_capacity=int(m[8]), wrapped=bool(m[9]),
                         elapsed_us=int(m[10]))
        elif (m := RE_DELTAS.search(line)) is not None:
            meter.update(delta_n=int(m[1]), delta_min_us=int(m[2]), delta_max_us=int(m[3]))
        elif (m := RE_HANDOFF.search(line)) is not None:
            meter.update(handoffs=int(m[1]), handoff_min_us=int(m[2]),
                         handoff_mean_us=int(m[3]), handoff_max_us=int(m[4]))
        elif (m := RE_LOOP.search(line)) is not None:
            meter.update(loops=int(m[1]), loop_min_us=int(m[2]), loop_mean_us=int(m[3]),
                         loop_max_us=int(m[4]))
        elif (m := RE_INPUT.search(line)) is not None:
            meter["inputs"].append({"i": int(m[1]), "delta_us": int(m[2]), "gen": int(m[3]),
                                    "neutral": bool(m[4]), "state": m[5]})
    return meter


def delivery(changes: int, inputs: int) -> float:
    """The share of the executor's input changes the console was actually notified.

    This is the honest collapse metric, and the bench's first attempt at one was
    not: matching notified bytes back to plan records *cannot* work in general,
    because a plan may revisit a state (every synthetic case here does, and so
    does the real 纠错宏 — 47 distinct states across 71 records). A 5 ms macro
    aliases against the 10 ms report period into a stream that still looks like a
    walk through the plan while 95% of its inputs are never seen. Counting
    state changes on both sides of the seam has no such blind spot.
    """
    return 0.0 if changes <= 0 else inputs / changes


def sample_stats(deltas: list[int]) -> dict:
    """min/p50/p95/max over the intervals the meter printed, in milliseconds.

    The meter deliberately keeps no percentiles: a 1 ms histogram cannot resolve a
    26-second loop's holds, and the first cut of this instrument reported a bucket
    bound as a median — which is a wrong number that looks like a right one. It
    prints the exact extremes over the whole run and the newest 64 intervals; this
    is where a distribution over that sample is computed, and `n` says how big the
    sample is.
    """
    ordered = sorted(deltas)
    if not ordered:
        return {"n": 0}
    return {"n": len(ordered), "min": ordered[0], "max": ordered[-1],
            "p50": ordered[len(ordered) // 2], "p95": ordered[(len(ordered) * 95) // 100]}


def console_interval_ms(noise: list[bytes]) -> float | None:
    """The console's own connection interval, from the same capture.

    §12.2 validation 3 asks for the input-to-input latency *against the console's
    own 5 ms link*, so the link's interval has to be in the evidence rather than
    quoted from another session. `gap.c` logs it at INFO on every connection for
    exactly this comparison, and §9.1 measured `conn_itvl=4` on 40/40 samples.
    """
    text = b"".join(noise).decode("utf-8", "replace")
    found = RE_CONN_ITVL.search(text)
    if found is None:
        return None
    # `conn_itvl` is in 1.25 ms units; the device prints the millisecond form too,
    # so a disagreement between the two is a firmware bug rather than a parse
    # question, and the caller is told by the mismatch.
    return int(found[1]) * 1.25


def hold_stats(records: list[dict]) -> dict:
    """The plan's own hold distribution, for the interval comparison.

    Compared as a distribution rather than record by record, for the same reason:
    the plan's holds are what the macro asked for, and the measured deltas are
    what the console saw. A positional pairing would be inventing precision.
    """
    holds = sorted(record["hold_ms"] for record in records)
    if not holds:
        return {"n": 0, "min": 0, "p50": 0, "max": 0}
    return {"n": len(holds), "min": holds[0], "p50": holds[len(holds) // 2], "max": holds[-1]}


def poll_while_running(link: ControlLink, seconds: float, poll_hz: float) -> dict:
    period = 1.0 / poll_hz
    first = None
    last = None
    first_at = last_at = 0.0
    frames = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = status_of(link)
        now = time.monotonic()
        if first is None:
            first, first_at = status, now
        last, last_at = status, now
        frames.add(status["current_frame"])
        time.sleep(period)
    return {"first": first, "last": last, "first_at": first_at, "last_at": last_at,
            "distinct_frames": sorted(frames)}


def run_case(link: ControlLink, name: str, plan, seconds: float, poll_hz: float,
             console_interval: float | None = None) -> dict:
    identity = plan_identity(plan.payload)
    load_plan(link, plan.payload, identity)
    before = status_of(link)
    if before["plan_hash"] != identity:
        raise RuntimeError(f"{name}: the echoed plan_hash is not what was sent (§5.6)")

    link.request(VERB_START)
    started = status_of(link)
    if started["mode"] != MODE_MACRO:
        raise RuntimeError(f"{name}: START did not enter MACRO (mode={started['mode']})")

    run = poll_while_running(link, seconds, poll_hz)

    # The meter block is printed by the mode exit, so it lands just after the
    # STOP reply — and it costs the link ~0.4 s, because the exit prints it
    # inside the same synchronous UART the reply waits behind (64 short lines at
    # 115200). The container's own request timeout is 2 s, so this is a longer
    # wait rather than a failure. Clear the capture, stop, then drain past it;
    # the trailing non-frame remainder needs an explicit flush, because a log
    # block with no frame behind it stays in the read buffer otherwise.
    link.noise_log.clear()
    link.request(VERB_STOP, timeout=3.0)
    link.drain(3.0)
    link.flush_noise()
    stopped = status_of(link)

    meter = parse_meter(link.noise_log)
    if stopped["mode"] != MODE_IDLE:
        raise RuntimeError(f"{name}: STOP did not return to IDLE (mode={stopped['mode']})")

    return {"name": name, "records": plan_records(plan.payload), "plan": plan,
            "run": run, "meter": meter, "console_link": run["last"]["console_link"],
            "console_interval_ms": console_interval}


def report_case(case: dict, verbose: bool, expect_console: bool = True) -> list[str]:
    failures: list[str] = []
    records = case["records"]
    meter = case["meter"]
    inputs = meter.get("inputs", [])
    planned = hold_stats(records)

    print(f"\n=== {case['name']} ===")
    print(f"    plan: {case['plan'].record_count} records, loop_ms={case['plan'].loop_ms}, "
          f"{len(case['plan'].payload)} B, holds {planned['min']}–{planned['max']} ms "
          f"(p50 {planned['p50']}); console_link="
          f"{CONSOLE_LINK_NAMES.get(case['console_link'], case['console_link'])}")
    if meter.get("notified") is None:
        failures.append(f"{case['name']}: no macro meter block was captured")
        print("    NO METER BLOCK — the exit did not print one (see the raw capture)")
        return failures

    elapsed_s = meter["elapsed_us"] / 1e6 if meter["elapsed_us"] else 0.0
    rate = (meter["notified"] / elapsed_s) if elapsed_s else 0.0
    print(f"    device: applied={meter['applied']} changes={meter['changes']} "
          f"inputs={meter['inputs_total']} notified={meter['notified']} "
          f"dropped={meter['dropped']} failed={meter['failed']} "
          f"ring={meter['ring']}/{meter['ring_capacity']}"
          f"{' (wrapped)' if meter.get('wrapped') else ''}")
    if rate:
        print(f"    report cadence: {rate:.1f}/s over {elapsed_s:.2f}s "
              f"(period {1000.0 / rate:.2f} ms; CONFIG_HID_REPORT_INTERVAL says 15 ms, "
              f"which is one 10 ms tick at CONFIG_FREERTOS_HZ=100)")
    else:
        print("    report cadence: nothing was notified")

    # The collapse metric: what the executor produced against what the console saw.
    delivered = delivery(meter["changes"], meter["inputs_total"])
    print(f"    delivery: {meter['inputs_total']}/{meter['changes']} input changes "
          f"({delivered * 100:.1f}%) — {meter['changes'] - meter['inputs_total']} lost")

    # The run's first input has no predecessor, so its delta is 0 by definition
    # and would drag every percentile down. It is skipped; the device's own
    # extremes skip it the same way.
    sample = sample_stats([entry["delta_us"] for entry in inputs if entry["i"] != 0])
    if sample["n"]:
        print(f"    measured deltas, newest {sample['n']}: min={sample['min'] / 1000:.2f} "
              f"p50={sample['p50'] / 1000:.2f} p95={sample['p95'] / 1000:.2f} "
              f"max={sample['max'] / 1000:.2f} ms (whole run: min="
              f"{meter['delta_min_us'] / 1000:.2f} max={meter['delta_max_us'] / 1000:.2f} "
              f"over {meter['delta_n']} intervals)")
        print(f"    vs planned holds {planned['min']}/{planned['p50']}/{planned['max']} ms "
              f"(min/p50/max)")
    if meter.get("handoffs"):
        # §4.6's bound is 50 ms and #24 could only bound it; the wait is the arm's
        # plus one per loop boundary, so `n` counts both.
        print(f"    handoff: {meter['handoffs']} waits (arm + boundaries), "
              f"min={meter['handoff_min_us'] / 1000:.2f} "
              f"mean={meter['handoff_mean_us'] / 1000:.2f} "
              f"max={meter['handoff_max_us'] / 1000:.2f} ms")
    if meter.get("loops"):
        # §5.4's "no inter-loop gap" as the report carries it: the interval
        # between two record-0 applies. The plan's own clock is exact by
        # construction, so this is the only place the boundary's cost shows.
        drift = meter["loop_mean_us"] / 1000.0 - case["plan"].loop_ms
        print(f"    loop period at the report: {meter['loops']} loops, "
              f"min={meter['loop_min_us'] / 1000:.1f} mean={meter['loop_mean_us'] / 1000:.1f} "
              f"max={meter['loop_max_us'] / 1000:.1f} ms vs plan {case['plan'].loop_ms} ms "
              f"({drift:+.1f} ms/loop)")
    if case.get("console_interval_ms") is not None:
        print(f"    the console's own link: {case['console_interval_ms']:.2f} ms "
              f"(conn_itvl from the same capture) — the report period is the constraint, "
              f"not the link")

    run = case["run"]
    print(f"    loop_count={run['last']['loop_count']} reached while polling; "
          f"{len(run['distinct_frames'])} distinct current_frame values seen")

    if verbose:
        print("    the notified inputs, in wire order (d = since the previous distinct "
              "state):")
        for entry in inputs:
            mark = " neutral (§4.6)" if entry["neutral"] else ""
            print(f"      #{entry['i']:<3} d={entry['delta_us'] / 1000:>8.2f} ms  "
                  f"gen={entry['gen']}{mark}")

    if expect_console and case["console_interval_ms"] is None:
        failures.append(f"{case['name']}: no connection-interval line in the capture — "
                        "the console's own link is half the comparison")
    if case["console_link"] == 1 and meter["notified"] == 0:
        failures.append(f"{case['name']}: the console was CONNECTED but nothing was notified")
    if not expect_console and case["console_link"] == 1:
        # A `--no-console` run is only an observation of an absent console if the
        # console stayed absent. The NS2 in standby reconnects to a bonded
        # controller on its own (§9.1: 20 connections over 19 disconnects), so
        # this is the common way to lose the window — and a case that ran with a
        # console attached must never be quoted as the absent-console behaviour.
        failures.append(f"{case['name']}: the console connected during a --no-console run")
    if meter["changes"] and meter["inputs_total"] > meter["changes"]:
        failures.append(f"{case['name']}: more inputs than changes — the meter is inconsistent")
    return failures


def wait_for_console(link: ControlLink, seconds: float) -> tuple[dict, float | None] | None:
    """Waits until a real console is on the link, then lets it finish subscribing.

    This is not politeness, it is the measurement. The report task skips the swap
    when nothing is subscribed, so a run started before the console's CCCD write
    has inputs the console never saw — and those look exactly like the collapse
    this ticket is looking for (the bench found that the hard way: records 0 and
    10 appeared "swallowed" simply because the console connected a second into
    the first case). §9.1: the console re-runs its whole init and subscribes
    itself on every connection, so waiting for `CONNECTED` plus a settle is what
    "the console is listening" means from here.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if status_of(link)["console_link"] == 1:
            time.sleep(2.0)
            # The connection happened during this wait, so its interval line is in
            # the noise the polls kept — and it belongs to the *session*, not to a
            # case (each case clears the capture before it stops).
            return status_of(link), console_interval_ms(link.noise_log)
        time.sleep(0.5)
    return None


def bench(port: str, seconds: float, poll_hz: float, library: Path | None,
          no_console: bool, verbose: bool, baud: int) -> int:
    failures: list[str] = []
    link = ControlLink(port, baud, capture_noise=True)
    try:
        hello = parse_hello(link.request(VERB_HELLO, bytes([1])).payload)
        print(f"HELLO: boot_id=0x{hello['boot_id']:08x} fw={'.'.join(map(str, hello['fw']))} "
              f"features=0x{hello['features']:04x} plan_capacity={hello['capacity']} B")
        if not (hello["features"] & 0x0001):
            failures.append("features.macro is not advertised — this firmware has no MACRO mode")
            return 1

        initial = status_of(link)
        print(f"before the runs: console_link="
              f"{CONSOLE_LINK_NAMES.get(initial['console_link'], initial['console_link'])} "
              f"bond={initial['bond']} mode={initial['mode']}")
        if no_console:
            if initial["console_link"] == 1:
                failures.append("--no-console was asked for, but a console is connected")
                print("!! the no-console observation needs the console absent; failing")
                return 1
            print("no console, as asked: the runs below should read notified=0 (G-4)")
        else:
            found = wait_for_console(link, 30.0)
            if found is None:
                failures.append("no console connected within 30 s — nothing to measure")
                print("!! no console on the link: wake the NS2 (Change Grip/Order if it has "
                      "forgotten the bond), or pass --no-console to record G-4")
                return 1
            connected, interval_ms = found
            if interval_ms is None:
                failures.append("the console connected but its connection interval was never "
                                "logged — half of validation 3 is the comparison against it")
                print("!! no conn_itvl line in the capture; the console's own link is the "
                      "baseline validation 3 compares the report period against")
            print(f"console connected: bond={connected['bond']}, "
                  f"its link at {interval_ms if interval_ms is None else f'{interval_ms:.2f}'} ms, "
                  f"settling done")

        session_interval = None if no_console else interval_ms
        for name, plan in cases(seconds, library):
            case = run_case(link, name, plan, seconds, poll_hz,
                            console_interval=session_interval)
            failures += report_case(case, verbose, expect_console=not no_console)

        final = status_of(link)
        print(f"\nafter the runs: mode={final['mode']} "
              f"last_stop_reason={final['last_stop_reason']} last_error={final['last_error']}")
    finally:
        link.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("macro timing bench: every case produced a meter block")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="how long each case runs before STOP (default 5)")
    ap.add_argument("--poll-hz", type=float, default=40.0,
                    help="STATUS poll rate while a case runs (default 40)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every record-to-record pair, not just the summary")
    ap.add_argument("--no-console", action="store_true",
                    help="G-4: the console is absent, and the run should say so")
    ap.add_argument("--macro", type=Path,
                    default=Path(os.path.expanduser("~/clone/switch-controller-macro/宏/纠错宏.json")),
                    help="a real library macro to run at its own holds")
    args = ap.parse_args()
    return bench(args.port, args.seconds, args.poll_hz, args.macro, args.no_console,
                 args.verbose, args.baud)


if __name__ == "__main__":
    raise SystemExit(main())
