#!/usr/bin/env python3
"""Measure whether the 256 B RX ring absorbs the §2.7 ACK window (issue #34).

§12.2 validation row 2: *does the 256 B RX ring at a 100 Hz tick actually
absorb the ACK window?* The answer this script produces is the four numbers
the ticket asks to record — **the ring size, the ACK window that holds, the
measured stall count, and the largest plan that transfers cleanly** — plus the
one thing a retry counter alone cannot say: **what the ring itself read**.

Two instruments, one answer:

- **the host side** is #33's counting uploader, unchanged: the synchronous
  mirror of the container's `FrameIO.bulk` (`scripts/bench_baud_flood.py::
  upload_plan_counted`), whose `window_retries` is the ticket's stall count —
  an ACK stall timed out and the uploader resent from the ACK'd offset
  (§2.7 rule 5 / ADR-0015).
- **the device side** is the #34 firmware instrument: one INFO line per bulk
  transfer, printed as staging closes —
  `staging closed: total=65528B ring hw=255/256 spins=180842 backlog=232/256 drops=0`
  — where `ring hw` is the zc ring's wrap-aware occupancy high-water *for that
  transfer*, `spins` the producer's yield-loop volume while it was full,
  `backlog` the UART driver-ring backlog high-water (the tier that actually
  drops bytes), and `drops` the decoder's silent-drop delta (§2.8). The line
  rides the log noise the link already counts and skips (§2.2), so the bench
  harvests it from the captured noise segments.

Why the device side matters: the ticket's own comment warns that a stall
through the OrbStack-forwarded path may be the forwarder, not the ring — and a
host-side zero proves less than it seems unless something proves the meter can
read pressure at all. So the bench runs two expectations:

- `--expect clean` (the design point, 115200): every stall, every ring-full
  event, every silent meter is a failure;
- `--expect degenerate` (the 921600 positive control): stalls are the point,
  and the run *fails* if the meter reads no ring pressure, because a meter
  that cannot see saturation cannot certify absorption either.

Uploads: the **real macro library** (`$REFERENCE_ROOT/switch-controller-macro/
宏`, the §5.7 four-row table's source — 杏仁巢穴宏 at 3,356 B is the largest
real plan) and a **synthetic ladder** topping out at 65,528 B, the largest
plan the advertised 65,536 B capacity accepts (12 + 11·5956; 5957 would need
65,539). Every commit is verified against `STATUS` (`plan_hash` echo,
`frame_count`, `IDLE`), and `boot_id` is read before and after: one value
means the link never reset.

Usage:
    python3 scripts/bench_rx_ring.py [--port /dev/cu.usbmodemXXXX] [--baud 115200]
                                     [--expect clean] [--repeats 2] [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_baud_flood import (  # noqa: E402
    BULK_RETRIES,
    BULK_TIMEOUT,
    PLAN_MAX_RECORDS,
    REQUEST_TIMEOUT,
    hello_boot_id,
    synthesize_plan,
    upload_plan_counted,
)
from bench_plan_executor import (  # noqa: E402
    ACK_WINDOW,
    MODE_IDLE,
    PLAN_COMMITTED,
    VERB_STATUS,
    ControlLink,
    parse_status,
)

# The device instrument's exact wire format (control_parser.c): one INFO line
# per staging close. The skin is ESP_LOG's; the payload is the contract. The
# four numbers and what each answers of "does the ring absorb the window":
#   ring hw  — zc-ring occupancy high-water; 255/256 is the blast's steady
#              state (a 256 B chunk frame out-sizes the ring), not a hazard
#   spins    — producer yield-loop volume while the ring was full (context)
#   backlog  — UART-driver RX backlog high-water: the tier that drops bytes;
#              ≪ capacity is the parser keeping pace, == capacity is the cliff
#   drops    — the decoder's silent-drop delta (§2.8): frames that arrived but
#              could not be trusted — the number that separates "ring full but
#              nothing lost" from the data loss §7.5 warns about
RING_LINE_RE = re.compile(
    r"staging closed: total=(\d+)B ring hw=(\d+)/(\d+) spins=(\d+) "
    r"backlog=(\d+)/(\d+) drops=(\d+)")

# §5.7's largest real plan, the anchor the loader asserts against: 杏仁巢穴宏,
# 411 events → 304 records, 3,356 B, identity d2717773e32486c9.
REAL_LARGEST = dict(name="杏仁巢穴宏", bytes=3356, records=304,
                    identity16="d2717773e32486c9")


# ── the device-side meter, harvested from the log noise ──────────────────────


def parse_ring_line(text: str) -> dict | None:
    """One instrument line → its numbers, or None if it is not one.

    Accepts ESP_LOG skin ahead of the payload (`I (618932) control: …`) and
    requires the whole payload: a truncated line is no reading, and a missing
    reading is a finding, never a zero.
    """
    match = RING_LINE_RE.search(text)
    if match is None or not re.search(r"^[IWE] \(\d+\) \S+: ", text):
        return None
    total, hw, cap, spins, backlog, bcap, drops = (int(g) for g in match.groups())
    return dict(total=total, hw=hw, capacity=cap, spins=spins,
                backlog=backlog, backlog_capacity=bcap, drops=drops)


def scan_ring_lines(noise: bytes) -> list[dict]:
    """Every instrument line in a noise capture, in wire order.

    Noise segments are joined with a newline, not concatenated: a segment
    that a frame's delimiter cut mid-line (the boot banner's tail is the
    usual one) has no trailing newline of its own, and concatenating it onto
    the next segment would glue two lines together — which is exactly how a
    real instrument line was first lost (the run's first transfer, right
    after the boot banner). A log line is written atomically under the shared
    TX lock (§2.2), so a line is never itself split across segments.
    """
    text = noise.decode("utf-8", "replace")
    return [m for m in (parse_ring_line(line) for line in text.splitlines())
            if m is not None]


# ── one transfer, both instruments ───────────────────────────────────────────


def run_transfer(link, *, label: str, payload: bytes, identity: bytes,
                 request_timeout: float = REQUEST_TIMEOUT,
                 ack_timeout: float = BULK_TIMEOUT,
                 max_retries: int = BULK_RETRIES) -> dict:
    """Upload one plan and return the row that carries both meters.

    The uploader is #33's `upload_plan_counted`; this adds the STATUS
    confirmation and attributes the transfer's instrument line, which the
    device prints as staging closes — ahead of the commit reply on the wire,
    so it is always readable by the time the STATUS reply has come back.
    """
    noise_log = getattr(link, "noise_log", None)
    seen_before = len(scan_ring_lines(b"\n".join(noise_log))) if noise_log else 0
    counters_before = dict(link.counters)

    stats = upload_plan_counted(link, payload, identity,
                                ack_timeout=ack_timeout, max_retries=max_retries)
    st = parse_status(link.request(VERB_STATUS, timeout=request_timeout).payload)
    records = (len(payload) - 12) // 11
    status_ok = (st["plan_state"] == PLAN_COMMITTED and st["plan_hash"] == identity
                 and st["frame_count"] == records and st["mode"] == MODE_IDLE)

    lines = scan_ring_lines(b"\n".join(noise_log))[seen_before:] if noise_log else []
    mine = lines[-1] if lines else None

    return dict(
        label=label, bytes=len(payload), records=records, status_ok=status_ok,
        chunk_frames=stats.chunk_frames, window_retries=stats.window_retries,
        resent_bytes=stats.resent_bytes, dup_acks=stats.dup_acks,
        early_acks=stats.early_acks,
        rx_bad_crc=link.counters["bad_crc"] - counters_before["bad_crc"],
        ring_hw=mine["hw"] if mine else None,
        ring_spins=mine["spins"] if mine else None,
        backlog_hw=mine["backlog"] if mine else None,
        backlog_capacity=mine["backlog_capacity"] if mine else None,
        ring_drops=mine["drops"] if mine else None,
        ring_capacity=mine["capacity"] if mine else None,
        ring_line_found=mine is not None and mine["total"] == len(payload),
        throughput=len(payload) / stats.elapsed if stats.elapsed else 0.0,
    )


# ── the verdict, in the ticket's terms ───────────────────────────────────────


def verdict(rows: list[dict], *, expect: str, window: int = ACK_WINDOW) -> dict:
    """Ring size, window, stall count, largest clean transfer — and failures.

    `clean` is the design point: absorption means zero stalls, **zero
    untrusted frames** (`drops` — the decoder saw nothing it had to drop), a
    backlog that never reached the driver ring's capacity (surviving at
    backlog == capacity is surviving by luck, not margin), and a meter that
    spoke for every transfer. `degenerate` is the positive control: it fails
    only if the transfers did not complete or the meter read no loss — a
    saturated line with a meter that cannot see loss would make every clean
    zero meaningless.
    """
    failures: list[str] = []
    ring_holds = True
    caps = {r["ring_capacity"] for r in rows if r["ring_line_found"]}
    ring_bytes = caps.pop() if len(caps) == 1 else None
    if ring_bytes is None:
        failures.append("ring size not established: the instrument's lines are "
                        "missing or disagree")
        ring_holds = False
    stall_count = sum(r["window_retries"] for r in rows)
    drops_total = sum(r["ring_drops"] or 0 for r in rows)
    backlog_hw = max((r["backlog_hw"] for r in rows if r["backlog_hw"] is not None),
                     default=None)
    backlog_cap = max((r["backlog_capacity"] for r in rows
                       if r["backlog_capacity"] is not None), default=None)
    max_hw = max((r["ring_hw"] for r in rows if r["ring_hw"] is not None),
                 default=None)
    clean = [r for r in rows
             if r["status_ok"] and r["window_retries"] == 0 and not r["ring_drops"]]
    largest_clean = max((r["bytes"] for r in clean), default=0)

    for r in rows:
        if not r["status_ok"]:
            failures.append(f"{r['label']}: STATUS did not confirm the commit")
            ring_holds = False
        if not r["ring_line_found"]:
            failures.append(f"{r['label']}: the ring instrument was silent")
            ring_holds = False
        if r["rx_bad_crc"]:
            failures.append(f"{r['label']}: {r['rx_bad_crc']} bad-CRC frames received")
            ring_holds = False

    if expect == "clean":
        for r in rows:
            if r["window_retries"]:
                failures.append(f"{r['label']}: {r['window_retries']} ACK stall(s)")
                ring_holds = False
            if r["ring_drops"]:
                failures.append(f"{r['label']}: {r['ring_drops']} frame(s) the device "
                                f"could not trust")
                ring_holds = False
            if r["backlog_hw"] is not None and r["backlog_capacity"] is not None \
                    and r["backlog_hw"] >= r["backlog_capacity"]:
                failures.append(f"{r['label']}: driver backlog reached its capacity "
                                f"({r['backlog_hw']}/{r['backlog_capacity']}) — the "
                                f"§7.5 cliff, survived by luck")
                ring_holds = False
    elif expect == "degenerate":
        ring_holds = False  # the control's rows are the failure mode itself
        if drops_total == 0:
            failures.append("positive control saw no untrusted frames — the meter "
                            "cannot certify a clean reading")
    else:
        raise ValueError(f"unknown expectation {expect!r}")

    return dict(ring_bytes=ring_bytes, ack_window=window, ring_holds=ring_holds,
                stall_count=stall_count, drops=drops_total, backlog_hw=backlog_hw,
                backlog_capacity=backlog_cap, max_hw=max_hw,
                largest_clean=largest_clean, transfers=len(rows), failures=failures)


# ── the real library (§5.7's table, compiled from the mount) ─────────────────


def load_real_plans(macro_dir: Path | str | None) -> list[tuple]:
    """Compile every macro under the reference mount, smallest plan first.

    Returns `(name, payload, identity, records)` tuples; a macro that rejects
    is skipped (its reason printed), an absent mount yields `[]` — the bench
    still runs its synthetic ladder, and the report says which it had.
    """
    if not macro_dir:
        return []
    macro_dir = Path(macro_dir)
    if not macro_dir.is_dir():
        return []

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
    from ns2plan.compiler import MacroRejected, compile_macro  # noqa: E402

    plans = []
    for path in sorted(macro_dir.glob("*.json")):
        try:
            plan = compile_macro(json.loads(path.read_text()))
        except (MacroRejected, ValueError, KeyError) as exc:
            print(f"  real library: {path.name} rejected ({exc}); skipped")
            continue
        plans.append((path.stem, plan.payload, plan.identity, plan.record_count))
    plans.sort(key=lambda p: len(p[1]))
    return plans


# ── the bench ─────────────────────────────────────────────────────────────────


def run_bench(port: str, *, baud: int, repeats: int, expect: str,
              real_dir: Path | None, records_ladder: list[int],
              json_path: str | None, ack_timeout: float, label: str) -> int:
    print(f"#34 bench{' [' + label + ']' if label else ''}: {port} at {baud} baud, "
          f"expect={expect}, window={ACK_WINDOW} B, "
          f"ladder {[12 + 11 * r for r in records_ladder]} B × {repeats}")
    link = ControlLink(port, baud, capture_noise=True)
    report: dict = {"port": port, "baud": baud, "expect": expect, "label": label,
                    "rows": []}
    try:
        boot_first = hello_boot_id(link)
        print(f"  boot_id=0x{boot_first:08x}")

        rows: list[dict] = []
        real = load_real_plans(real_dir)
        if real:
            largest = real[-1]
            anchor_ok = (largest[0] == REAL_LARGEST["name"]
                         and len(largest[1]) == REAL_LARGEST["bytes"]
                         and largest[3] == REAL_LARGEST["records"]
                         and largest[2].hex()[:16] == REAL_LARGEST["identity16"])
            print(f"  real library: {len(real)} plan(s), largest "
                  f"{largest[0]} at {len(largest[1])} B "
                  f"({'§5.7 anchor matches' if anchor_ok else 'ANCHOR MISMATCH'})")
            report["real_anchor_ok"] = anchor_ok
            if not anchor_ok:
                report.setdefault("notes", []).append(
                    "the reference mount's largest plan is not §5.7's 杏仁 row")
        else:
            print("  real library: not mounted; the synthetic ladder carries the run")
            report.setdefault("notes", []).append("real macro library absent")

        for name, payload, identity, _records in real:
            for i in range(repeats):
                rows.append(run_transfer(link, label=f"real-{name}×{i + 1}",
                                         payload=payload, identity=identity,
                                         ack_timeout=ack_timeout))
        for records in records_ladder:
            payload, identity = synthesize_plan(records)
            for i in range(repeats):
                rows.append(run_transfer(link, label=f"synthetic-{len(payload)}B×{i + 1}",
                                         payload=payload, identity=identity,
                                         ack_timeout=ack_timeout))

        for row in rows:
            mark = "ok " if (row["status_ok"] and row["ring_line_found"]) else "FAIL"
            if row["ring_line_found"]:
                ring = (f"hw={row['ring_hw']}/{row['ring_capacity']} "
                        f"spins={row['ring_spins']} "
                        f"backlog={row['backlog_hw']}/{row['backlog_capacity']} "
                        f"drops={row['ring_drops']}")
            else:
                ring = "meter silent"
            print(f"  bulk {row['label']:<24} {row['bytes']:>5} B: [{mark}] "
                  f"stalls={row['window_retries']} dup={row['dup_acks']} "
                  f"early={row['early_acks']} {ring} "
                  f"bad_crc={row['rx_bad_crc']} {row['throughput'] / 1024:.1f} KiB/s")
            report["rows"].append(row)

        boot_last = hello_boot_id(link)
        report["boot_first"] = boot_first
        report["boot_last"] = boot_last
        if boot_last != boot_first:
            report["verdict"] = None
            print(f"  final boot_id=0x{boot_last:08x} CHANGED from 0x{boot_first:08x}")
            report["failures"] = [f"boot_id changed: {boot_first:#x} → {boot_last:#x}"]
            return 1
        print(f"  final boot_id=0x{boot_last:08x} (same)")

        v = verdict(rows, expect=expect)
        report["verdict"] = v
        print(f"  verdict: ring={v['ring_bytes']} B, window={v['ack_window']} B "
              f"holds={v['ring_holds']}, stalls={v['stall_count']}, "
              f"drops={v['drops']}, backlog hw={v['backlog_hw']}/{v['backlog_capacity']}, "
              f"ring hw={v['max_hw']}, largest clean={v['largest_clean']} B")
        if v["failures"]:
            print(f"\nFAILED: {len(v['failures'])} finding(s)")
            for f in v["failures"]:
                print(f"  - {f}")
            return 1
        print(f"\n#34 bench ({expect}): all checks passed")
        return 0
    finally:
        link.close()
        if json_path:
            Path(json_path).write_text(json.dumps(report, indent=1) + "\n")
            print(f"  json: {json_path}")


def main() -> int:
    reference_root = Path(os.environ.get("REFERENCE_ROOT", Path.home() / "clone"))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--expect", choices=("clean", "degenerate"), default="clean",
                    help="clean = the design point (stalls fail); degenerate = the "
                         "921600 positive control (no ring pressure fails)")
    ap.add_argument("--ack-timeout", type=float, default=BULK_TIMEOUT,
                    help="per-window ACK wait; the container uses %.1f s" % BULK_TIMEOUT)
    ap.add_argument("--real-dir", type=Path,
                    default=reference_root / "switch-controller-macro" / "宏",
                    help="the reference macro mount; pass '' to skip the real plans")
    ap.add_argument("--records", default=f"48,371,{PLAN_MAX_RECORDS}",
                    help="comma-separated synthetic ladder in plan records "
                         "(default 48,371,5956 = 540 B, 4093 B, 65528 B)")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--label", default="", help="a tag carried into the report/json")
    args = ap.parse_args()
    ladder = [int(x) for x in args.records.split(",") if x.strip()]
    return run_bench(args.port, baud=args.baud, repeats=args.repeats,
                     expect=args.expect, real_dir=args.real_dir,
                     records_ladder=ladder, json_path=args.json,
                     ack_timeout=args.ack_timeout, label=args.label)


if __name__ == "__main__":
    raise SystemExit(main())
