#!/usr/bin/env python3
"""Measure the control link's baud under a log flood (issue #33, gap G-1).

§12.2 validation row 1: *does the control link work at 921600 with `ESP_LOG` on
the same wire?* The answer this script produces is a measurement, not an
opinion — the numbers that retire G-1:

- **the retry count** — window-granularity retries during `LOAD_PLAN` bulk
  transfers (§2.7 rule 5 / ADR-0015), which is how a frame lost to the flood
  manifests: §2.8 makes a CRC-failed frame *silent*, so a lost chunk surfaces as
  an ACK stall (timeout → resend from the ACK'd offset) or as the offset-keyed
  mismatch ACK the device answers the *next* chunk with;
- **frame loss** — `HELLO`s unanswered under the flood, window retries, and the
  host-side bad-CRC count (a reply split by a log line is exactly what the
  shared TX lock exists to prevent, so this must read 0);
- **the largest clean transfer** — the size ladder tops out at the largest plan
  the advertised 65536-byte capacity accepts (12 + 11·5955 = 65517 B);
- **link survival** — `boot_id` is re-read after every phase; one value means
  the link never reset.

The uploader is the synchronous mirror of the container's real one
(`container/ns2serial/frame_io.py::FrameIO.bulk`) — same window fill, same
resume-from-the-ACK'd-offset retry, the same 2 s request / 5 s bulk timeouts and
3-retry ceiling — so the retry counts are the deployment's, not an idealised
client's. `scripts/test_bench_baud_flood.py` pins that accounting against a fake
device that encodes §2.7.

Flash the flood build first (`CONFIG_MCU_DEBUG=y`, `CONFIG_LOG_MAXIMUM_LEVEL=4`
— the #21 recipe, `docs/research/control-link-stage1.md` §4.3): every received
frame draws DEBUG lines through the same `esp_log_set_vprintf` hook, and the
HID task's "skipping report send" line floods at report rate whenever no
console is subscribed. During a transfer the bulk policy drops those lines
after formatting them (§2.2) and lets one per 100 ms onto the wire — that
suppression, plus the TX lock, is precisely what is under test.

Usage:
    python3 scripts/bench_baud_flood.py [--port /dev/cu.usbmodemXXXX] [--baud 921600]
                                        [--repeats 2] [--flood-seconds 10] [--json PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_plan_executor import (  # noqa: E402
    CHUNK_SIZE,
    ControlLink,
    Frame,
    VERB_ERROR,
    VERB_HELLO,
    VERB_LOAD_PLAN,
    VERB_STATUS,
    parse_hello,
    parse_status,
)

# §2.7: the ACK window is a constant of the chapter, not a HELLO field.
ACK_WINDOW = 4096

# The container's FrameIO defaults (container/ns2serial/frame_io.py) — the
# deployment's patience, quoted here so the bench measures the real client.
REQUEST_TIMEOUT = 2.0
BULK_TIMEOUT = 5.0
BULK_RETRIES = 3

# plan.h, duplicated for synthesis (the C static_asserts pin the layout; the
# Python compiler produces the same bytes — fixtures/plan is the shared proof).
PLAN_MAGIC = 0x4C50534E  # little-endian "NSPL"
PLAN_HEADER_SIZE = 12
PLAN_RECORD_SIZE = 11
PLAN_CAPACITY_BYTES = 65536
# 12 + 11·n ≤ 65536 ⇒ n ≤ 5956.7 ⇒ 5956 records / 65528 B is the largest plan
# the advertised capacity accepts (5957 would need 65539 B).
PLAN_MAX_RECORDS = (PLAN_CAPACITY_BYTES - PLAN_HEADER_SIZE) // PLAN_RECORD_SIZE  # 5956

MODE_IDLE = 0
PLAN_COMMITTED = 1

BULK_ANNOUNCE, BULK_CHUNK, BULK_COMMIT = 1, 2, 3


class CommandError(RuntimeError):
    """The device answered an `ERROR` (verb 10) — a typed refusal, not a fault."""

    def __init__(self, where: str, code: int, detail: int):
        super().__init__(f"{where}: ERROR code={code} detail={detail}")
        self.code = code
        self.detail = detail


class ProtocolViolation(RuntimeError):
    """The device ACK'd an offset it cannot have."""


@dataclass
class UploadStats:
    """The G-1 accounting for one bulk transfer.

    `window_retries` is §2.7's retry at window granularity: an ACK stall timed
    out and the uploader resent from the ACK'd offset. `dup_acks` are ACKs that
    named an offset the uploader already knew (the device-side loss signal: the
    chunk after a dropped one draws the mismatch ACK), `early_acks` are mid-
    window progress ACKs — permitted (§2.7), never a fault.
    """

    chunk_frames: int = 0
    chunk_bytes: int = 0
    window_retries: int = 0
    resent_bytes: int = 0
    dup_acks: int = 0
    early_acks: int = 0
    elapsed: float = 0.0
    started: float = field(default_factory=time.monotonic)


def synthesize_plan(records: int, loop_ms: int = 0) -> tuple[bytes, bytes]:
    """A structurally valid §5.3 plan of exactly `records` records.

    Commit runs the structural check, never the executor, so the record bytes
    only need to be non-degenerate; the identity is §5.6's SHA-256 truncated to
    16 bytes.
    """
    header = struct.pack("<IBBHI", PLAN_MAGIC, 1, PLAN_RECORD_SIZE, records, loop_ms)
    payload = header + bytes((i * 31 + 7) & 0xFF for i in range(records * PLAN_RECORD_SIZE))
    return payload, hashlib.sha256(payload).digest()[:16]


def _reply_offset(reply: Frame, where: str) -> int:
    if reply.verb == VERB_ERROR:
        code = reply.payload[0] if reply.payload else -1
        (detail,) = struct.unpack_from("<I", reply.payload, 1) if len(reply.payload) >= 5 else (0,)
        raise CommandError(where, code, detail)
    if len(reply.payload) != 4:
        raise ProtocolViolation(f"{where}: reply is not an offset ACK: {reply.payload.hex()}")
    return struct.unpack("<I", reply.payload)[0]


def upload_plan_counted(
    link,
    payload: bytes,
    identity: bytes,
    *,
    window: int = ACK_WINDOW,
    chunk_size: int = CHUNK_SIZE,
    ack_timeout: float = BULK_TIMEOUT,
    max_retries: int = BULK_RETRIES,
    pace_ms: float = 0.0,
    stats: UploadStats | None = None,
) -> UploadStats:
    """The container's windowed bulk uploader (§2.7), synchronously, counted.

    Mirrors `FrameIO.bulk`: announce, fill one window of un-ACKed chunks, follow
    the ACK'd offset, retry at window granularity on a stall, commit, and
    require the commit ACK to name the whole transfer. `pace_ms` is bench-only
    mechanism evidence: 0 blasts the window back-to-back exactly as the
    container does; N inserts N ms between chunk writes (#24's client paced
    implicitly by waiting per chunk — the knob isolates that difference).
    """
    stats = stats if stats is not None else UploadStats()
    stats.started = time.monotonic()
    total = len(payload)

    reply = link.request(
        VERB_LOAD_PLAN, struct.pack("<BI", BULK_ANNOUNCE, total) + identity,
        timeout=REQUEST_TIMEOUT)
    acked = _reply_offset(reply, "announce")
    if acked > total:
        raise ProtocolViolation(f"announce ACK'd {acked} > total {total}")

    sent = 0
    retries = 0
    acked_at_last_timeout = 0
    while sent < total or acked < total:
        while sent < total and (sent - acked) < window:
            n = min(chunk_size, total - sent)
            link.send(VERB_LOAD_PLAN, struct.pack("<BI", BULK_CHUNK, sent) + payload[sent:sent + n])
            stats.chunk_frames += 1
            stats.chunk_bytes += n
            sent += n
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
        try:
            reply = link.wait_reply(VERB_LOAD_PLAN, ack_timeout)
        except TimeoutError:
            # The container resets its retry budget on any reply because its
            # read loop also carries liveness (a dead link raises from below,
            # TransportUnavailable). This client has no such layer, so the
            # budget rides "no ACK progress since the last timeout" instead —
            # identical counts on a lossy-but-alive wire, and a loud abort
            # rather than an infinite resend loop on a wedged one.
            retries = retries + 1 if acked == acked_at_last_timeout else 1
            acked_at_last_timeout = acked
            stats.window_retries += 1
            stats.resent_bytes += sent - acked
            if retries > max_retries:
                raise
            sent = acked  # window granularity: resend from next_expected_offset
            continue
        offset = _reply_offset(reply, "chunk")
        if offset < acked or offset > sent:
            raise ProtocolViolation(
                f"the device ACK'd {offset} outside [acked={acked}, sent={sent}]")
        if offset == acked:
            stats.dup_acks += 1
        elif offset < sent:
            stats.early_acks += 1
        if offset > acked:
            retries = 0  # only progress re-arms the budget; a dup ACK is not liveness
        acked = offset

    reply = link.request(
        VERB_LOAD_PLAN, struct.pack("<BI", BULK_COMMIT, total) + identity,
        timeout=REQUEST_TIMEOUT)
    final = _reply_offset(reply, "commit")
    if final != total:
        raise ProtocolViolation(f"commit ACK'd {final}, expected the whole {total} B")
    stats.elapsed = time.monotonic() - stats.started
    return stats


# ── the bench ─────────────────────────────────────────────────────────────────


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def ladder_phase(link: ControlLink, records: int, repeats: int, ack_timeout: float,
                 pace_ms: float = 0.0) -> list[dict]:
    rows: list[dict] = []
    payload, identity = synthesize_plan(records)
    for i in range(repeats):
        before = dict(link.counters)
        stats = upload_plan_counted(link, payload, identity, ack_timeout=ack_timeout,
                                    pace_ms=pace_ms)
        deltas = {k: link.counters[k] - before[k] for k in link.counters}
        st = parse_status(link.request(VERB_STATUS, timeout=REQUEST_TIMEOUT).payload)
        ok = (st["plan_state"] == PLAN_COMMITTED and st["plan_hash"] == identity
              and st["frame_count"] == records and st["mode"] == MODE_IDLE)
        rows.append(dict(
            bytes=len(payload), records=records, repeat=i + 1, status_ok=ok,
            **vars(stats), **{f"rx_{k}": v for k, v in deltas.items()},
            throughput=len(payload) / stats.elapsed if stats.elapsed else 0.0,
        ))
    return rows


def hello_boot_id(link: ControlLink) -> int:
    reply = link.request(VERB_HELLO, bytes([1]), timeout=REQUEST_TIMEOUT)
    return parse_hello(reply.payload)["boot_id"]


def run_bench(port: str, *, baud: int, repeats: int, flood_seconds: float,
              ack_timeout: float, records_ladder: list[int], json_path: str | None,
              pace_ms: float = 0.0, label: str = "") -> int:
    print(f"G-1 bench{' [' + label + ']' if label else ''}: {port} at {baud} baud, flood "
          f"{flood_seconds:.0f}s, pace={pace_ms} ms/chunk, "
          f"ladder {[12 + 11 * r for r in records_ladder]} B × {repeats}")
    link = ControlLink(port, baud)
    report: dict = {"port": port, "baud": baud, "pace_ms": pace_ms, "label": label,
                    "phases": []}
    failures: list[str] = []
    try:
        boot_first = hello_boot_id(link)
        print(f"  boot_id=0x{boot_first:08x}")

        # Phase 1 — the idle flood (the #21 §4.3 probe, same recipe).
        counters_before = dict(link.counters)
        t0 = time.monotonic()
        latencies: list[float] = []
        answered = 0
        boot_ids = {boot_first}
        deadline = time.monotonic() + flood_seconds
        sent = 0
        while time.monotonic() < deadline:
            sent += 1
            t_req = time.monotonic()
            try:
                reply = link.request(VERB_HELLO, bytes([1]), timeout=REQUEST_TIMEOUT)
            except TimeoutError:
                continue
            latencies.append(time.monotonic() - t_req)
            if reply.verb != VERB_ERROR:
                answered += 1
                boot_ids.add(parse_hello(reply.payload)["boot_id"])
        flood = dict(
            seconds=time.monotonic() - t0, sent=sent, answered=answered,
            lost=sent - answered,
            lat_ms=[1000 * v for v in latencies],
            boot_ids=sorted(boot_ids),
            **{f"rx_{k}": link.counters[k] - counters_before[k] for k in link.counters},
        )
        report["phases"].append({"flood_idle": flood})
        lat = flood["lat_ms"]
        print(f"  flood-idle: {answered}/{sent} answered, lost={flood['lost']}, "
              f"bad_crc={flood['rx_bad_crc']}, boot_ids={len(flood['boot_ids'])}")
        if lat:
            print(f"    latency ms: min={min(lat):.1f} med={_percentile(lat, 0.5):.1f} "
                  f"p95={_percentile(lat, 0.95):.1f} max={max(lat):.1f}")
        if flood["rx_bad_crc"] != 0:
            failures.append(f"flood-idle: {flood['rx_bad_crc']} replies with a bad CRC")
        if len(flood["boot_ids"]) != 1:
            failures.append(f"flood-idle: boot_id changed: {flood['boot_ids']}")
        if flood["lost"] > max(2, sent // 20):
            failures.append(f"flood-idle: {flood['lost']}/{sent} HELLOs lost")

        # Phase 2 — the size ladder under the firmware's own bulk-time flood.
        all_rows: list[dict] = []
        for records in records_ladder:
            rows = ladder_phase(link, records, repeats, ack_timeout, pace_ms)
            all_rows.extend(rows)
            for row in rows:
                mark = "ok " if row["status_ok"] else "FAIL"
                print(f"  bulk {row['bytes']:>5} B ×{row['repeat']}: [{mark}] "
                      f"retries={row['window_retries']} dup={row['dup_acks']} "
                      f"early={row['early_acks']} resent={row['resent_bytes']} B "
                      f"bad_crc={row['rx_bad_crc']} "
                      f"{row['throughput'] / 1024:.1f} KiB/s")
                if not row["status_ok"]:
                    failures.append(f"bulk {row['bytes']} B ×{row['repeat']}: "
                                    f"STATUS did not confirm the commit")
                if row["rx_bad_crc"] != 0:
                    failures.append(f"bulk {row['bytes']} B ×{row['repeat']}: "
                                    f"{row['rx_bad_crc']} bad-CRC frames received")
        report["phases"].append({"ladder": all_rows})
        total_retries = sum(r["window_retries"] for r in all_rows)
        total_resent = sum(r["resent_bytes"] for r in all_rows)
        print(f"  ladder totals: retries={total_retries}, resent={total_resent} B, "
              f"transfers={len(all_rows)}")

        # Phase 3 — the wire is still a link: same boot_id, STATUS answers.
        boot_last = hello_boot_id(link)
        report["boot_first"] = boot_first
        report["boot_last"] = boot_last
        if boot_last != boot_first:
            failures.append(f"boot_id changed across the bench: {boot_first:#x} → {boot_last:#x}")
        print(f"  final boot_id=0x{boot_last:08x} "
              f"({'same' if boot_last == boot_first else 'CHANGED'})")
    finally:
        link.close()

    report["failures"] = failures
    if json_path:
        Path(json_path).write_text(json.dumps(report, indent=1) + "\n")
        print(f"  json: {json_path}")
    if failures:
        print(f"\nFAILED: {len(failures)} finding(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nbaud bench: all checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--repeats", type=int, default=2,
                    help="repeats per ladder size (default 2)")
    ap.add_argument("--flood-seconds", type=float, default=10.0)
    ap.add_argument("--ack-timeout", type=float, default=BULK_TIMEOUT,
                    help="per-window ACK wait; the container uses %.1f s" % BULK_TIMEOUT)
    ap.add_argument("--pace-ms", type=float, default=0.0,
                    help="bench-only: N ms between chunk writes (0 = the container's "
                         "back-to-back window blast; #24's client paced implicitly)")
    ap.add_argument("--records", default=f"48,371,1486,2973,{PLAN_MAX_RECORDS}",
                    help="comma-separated ladder in plan records (default 48..5955)")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--label", default="", help="a tag carried into the report/json")
    args = ap.parse_args()
    ladder = [int(x) for x in args.records.split(",") if x.strip()]
    return run_bench(args.port, baud=args.baud, repeats=args.repeats,
                     flood_seconds=args.flood_seconds, ack_timeout=args.ack_timeout,
                     records_ladder=ladder, json_path=args.json, pace_ms=args.pace_ms,
                     label=args.label)


if __name__ == "__main__":
    raise SystemExit(main())
