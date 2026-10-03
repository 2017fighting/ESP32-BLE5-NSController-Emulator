#!/usr/bin/env python3
"""Bench the mid-press drop and the release-build reboot rate (issue #38).

Two §12.2 questions, one session, one container:

**Half A — validation row 7 / G-5: is a pass resumed mid-press after a
console-link drop a problem?** The container's stop-on-drop policy (§9.3) exists
so it should never bite. This half *presents the condition* and measures what
happens: a macro that holds A for 60 s of every 60 s loop, the console put to
sleep while that press is on the wire, and then:

  * the drop lands with `mode=MACRO` and the press record current — the
    condition, not a hope about it;
  * the container sends exactly one `STOP` and the device reads `IDLE` with a
    plain `CONTAINER_STOP` on the wire and `CONSOLE_LOST` only in the UI;
  * the device's own report-cadence meter shows the run was carrying a
    non-neutral state at the drop, and that the stop's neutral was written
    after the last notified input (the readout's `changes - inputs`);
  * on reconnect the run does **not** resume, no second `START` goes out, and
    the drop story survives — so nothing mid-press reaches the freshly-connected
    console.

The device-side "neutral is the last write of the mode" is pinned offline, not
here: `test/host/test_control_executor.c:493` (`test_stop_commits_the_neutral`).
This half proves the *condition* was real and that the console never sees it.

**Half B — validation row 8 / G-7's second half: the release-build reboot
rate.** §9.2's 8/19 is a DEBUG-era figure on the pre-fix console path (§7.6,
`advertise-restart-bench.md` §1). This half runs sleep/wake cycles on the
shipped release build (`CONFIG_MCU_DEBUG` off, `LOG_MAXIMUM_LEVEL=INFO`, 115200)
and counts, in the same log stream the container already reads:

  * `reason=531` disconnects — the cycles, the denominator;
  * `boot_id` changes (`recovery: the device reported a boot`) — the numerator;
  * `stack overflow in task Tmr Svc` — the pre-fix defect's own words, should
    the fix ever stop holding.

The ticket's "compare" is against 8 overflows / 8 reboots in 19 disconnects, the
DEBUG build's number. A rate of 0/N is the expected release-build answer with
the §7.6 fix in; N is what makes it a measurement rather than a hope.

**Human actions** (the script prints one as it reaches each, and waits):

  A2  wake the console so the link connects               (≤240 s)
  A4  put the console to sleep while the press is held    (≤240 s)
  A6  wake the console again                              (≤240 s)
  B1  sleep/wake the console `--cycles` times             (≤240 s each)

Usage:
    container/.venv/bin/python scripts/bench_link_drop.py --cycles 20
    container/.venv/bin/python scripts/bench_link_drop.py --skip-mid-press
    container/.venv/bin/python scripts/bench_link_drop.py --skip-cycles --out /tmp/row.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from container.ns2plan import iter_frames

#: §4.6's template — the nine state bytes a stopped run must leave in the report:
#: buttons `00 00 00`, left stick centred `00 08 80`, right stick centred `00 08 80`.
NEUTRAL = bytes([0x00, 0x00, 0x00, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80])

#: The mid-press macro, as the compiler's canonical input (§5.1). A is held for
#: 60 s of the 60 s loop, so *any* drop during the run is a mid-press drop and
#: the condition does not depend on human timing. It compiles to two records:
#: frame 0 `A pressed` hold 60000, frame 1 the neutral hold 0 (§5.2's legal
#: zero-hold last record), i.e. `loop_ms = 60000`.
MID_PRESS_JSON = [
    {"t": 0.0, "ev": {"type": "button", "name": "a", "pressed": True}},
    {"t": 60000.0, "ev": {"type": "button", "name": "a", "pressed": False}},
]

#: `macro meter: #3 d=10000us g=2 s=0000400008000008 80` — the newest-64 inputs
#: the run notified, each with its state and the `n=1` neutral flag.
_METER_INPUT = re.compile(r"#(\d+) d=(\d+)us g=(\d+)( n=1)? s=([0-9a-fA-F]{18})")

#: `macro meter: applied=.. changes=.. inputs=.. notified=.. dropped=.. failed=.. ring=n/64`.
_METER_SUMMARY = re.compile(
    r"applied=(\d+) changes=(\d+) inputs=(\d+) notified=(\d+) "
    r"dropped=(\d+) failed=(\d+) ring=(\d+)/(\d+)"
)

PHASE_TIMEOUT = 240.0
EVIDENCE: dict = {"phases": []}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ── the device log, read the way the Logs screen reads it ────────────────────


def parse_meter_input(message: str) -> dict | None:
    """One `macro meter: #k …` input line, or `None` for any other line."""
    match = _METER_INPUT.search(message)
    if match is None:
        return None
    return {
        "index": int(match.group(1)),
        "deltaUs": int(match.group(2)),
        "gen": int(match.group(3)),
        "neutral": match.group(4) is not None,
        "state": match.group(5),
    }


def parse_meter_summary(message: str) -> dict | None:
    """The run's one counter line, or `None` for any other line."""
    match = _METER_SUMMARY.search(message)
    if match is None:
        return None
    fields = (
        "applied",
        "changes",
        "inputs",
        "notified",
        "dropped",
        "failed",
        "ring",
        "capacity",
    )
    return {name: int(value) for name, value in zip(fields, match.groups())}


class Meter:
    """The run's readout as the device printed it, harvested from the log ring.

    One readout per run: `report()` disarms the meter, so the neutral the stop
    writes is *applied* (it counts in `applied`/`changes`) but is notified after
    the readout. That gap is the device-side evidence available here; the
    neutral's being the mode's last write is pinned offline.
    """

    def __init__(self) -> None:
        self.summary: dict | None = None
        self.inputs: list[dict] = []

    def feed(self, message: str) -> bool:
        summary = parse_meter_summary(message)
        if summary is not None:
            self.summary = summary
            return True
        item = parse_meter_input(message)
        if item is not None:
            self.inputs.append(item)
            return True
        return False

    @property
    def last_input(self) -> dict | None:
        return self.inputs[-1] if self.inputs else None

    @property
    def carried_a_press(self) -> bool:
        """True when at least one notified state was not the neutral template."""
        return any(item["state"].lower() != NEUTRAL.hex() for item in self.inputs)

    @property
    def neutral_pending(self) -> int | None:
        """`changes - inputs`: applied writes whose distinct state was not notified.

        The stop's neutral is one such write with the frame on a press, so a
        value >= 1 is what "the neutral was written after the last notified
        input" looks like — but it is not exactly 1: a re-subscribe mid-run
        re-arms the executor (neutral, then the current frame), and those
        applied transitions count here too. The bench's own run read 3.
        """
        if self.summary is None:
            return None
        return self.summary["changes"] - self.summary["inputs"]


def mid_press_failures(meter: Meter, dropped_frame: int | None) -> list[str]:
    """The half-A verdict: what the readout must show for the drop to count."""
    failures: list[str] = []
    if meter.summary is None:
        failures.append("the run printed no meter readout — the stop's readout was not captured")
        return failures
    if dropped_frame != 0:
        failures.append(f"the drop landed on frame {dropped_frame!r}, not the press record (0)")
    if not meter.carried_a_press:
        failures.append("the readout carries no non-neutral input — the drop was not mid-press")
    pending = meter.neutral_pending
    if pending is None or pending < 0:
        failures.append(f"changes - inputs = {pending!r}: the neutral write is not accounted for")
    return failures


class LogTally:
    """The counters half B reads off the one stream the container already reads."""

    def __init__(self) -> None:
        self.drops = 0
        self.reason531 = 0
        self.reconnects = 0
        self.boots = 0
        self.overflows = 0
        self.rst0xc = 0

    def feed(self, level: str, source: str, message: str) -> None:
        lowered = message.lower()
        if "console link: disconnected" in message:
            self.drops += 1
            # §9.1's fact, asserted rather than assumed: the console ends every
            # one of these itself (HCI 0x13 = 531), never a link timeout.
            if "reason=531" in message:
                self.reason531 += 1
        elif "console link: connected" in message or "console link: resubscribed" in message:
            self.reconnects += 1
        if "reported a boot" in message:
            self.boots += 1
        if "stack overflow in task tmr svc" in lowered:
            self.overflows += 1
        if "rst:0xc" in lowered:
            self.rst0xc += 1


async def wait_until(predicate, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    log(f"TIMEOUT waiting for {what} after {timeout:.0f}s")
    return False


async def user_starts_a_run(controller, macro_id: str, tries: int = 4) -> bool:
    """`START` the way §8.9 says a human may: never re-send a mode verb on a slow
    reply — read the truth, then decide (a console reconnect can drown the reply
    in the log flood; #31 §3.1 measured it)."""
    for attempt in range(1, tries + 1):
        try:
            await controller.start_macro(macro_id)
            return True
        except Exception as failure:  # noqa: BLE001 - the retry is the point
            log(f"  start attempt {attempt} failed: {failure}")
            await wait_until(
                lambda: controller.snapshot()["control"]["link"] == "UP", 15.0, "the link to return"
            )
            await asyncio.sleep(1.0)
            if controller.snapshot()["mode"] == "MACRO":
                log("  the START landed late — the run is already in MACRO")
                return True
    return controller.snapshot()["mode"] == "MACRO"


def record(phase: str, controller, extra: dict | None = None) -> dict:
    snapshot = controller.snapshot()
    row = {
        "phase": phase,
        "t": round(time.monotonic(), 3),
        "mode": snapshot["mode"],
        "consoleLink": snapshot["console"]["link"],
        "lastDropReason": snapshot["console"]["lastDropReason"],
        "stopReason": snapshot["stopReason"],
        "lastError": snapshot["lastError"],
        "recovery": snapshot["recovery"],
        "planHash": (snapshot["plan"] or {}).get("hash"),
        "loops": (snapshot["plan"] or {}).get("loopCount"),
        "frame": (snapshot["plan"] or {}).get("currentFrame"),
    }
    if extra:
        row.update(extra)
    EVIDENCE["phases"].append(row)
    log(f"{phase}: {json.dumps(row, ensure_ascii=False)}")
    return row


# ── the session ──────────────────────────────────────────────────────────────


class CountingVerb:
    """Wraps one device verb so 'sent once' is measured, not asserted."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.inner()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--cycles",
        type=int,
        default=20,
        help="sleep/wake cycles to count for the release-build reboot rate (half B)",
    )
    parser.add_argument("--skip-mid-press", action="store_true")
    parser.add_argument("--skip-cycles", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="write the evidence JSON here")
    args = parser.parse_args()

    # Imported here, not at module scope: the meter parsing above is stdlib and
    # the offline test imports this module without aiohttp or pyserial.
    from container.ns2container import Settings, create_controller

    tmp = tempfile.TemporaryDirectory()
    macro_dir = Path(tmp.name) / "macros"
    macro_dir.mkdir()
    (macro_dir / "mid_press.json").write_text(json.dumps(MID_PRESS_JSON))
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "plan" / "correction.json"
    shutil.copy(fixture, macro_dir / "correction.json")

    settings = Settings(
        port=args.port,
        baud=args.baud,
        macro_dir=macro_dir,
        amiibo_dir=Path(tmp.name) / "amiibo",
        key_file=Path(tmp.name) / "keys" / "key_retail.bin",
        key_dir=Path(tmp.name) / "keys",
        static_dir=Path(tmp.name) / "dist",
    )
    controller = create_controller(settings)
    await controller.start()

    tally = LogTally()
    meter = Meter()

    async def tee_logs() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for name, payload in controller.subscribe():
                if name == "log":
                    tally.feed(payload["level"], payload["source"], payload["message"])
                    meter.feed(payload["message"])
                    log(f"  ui-log [{payload['source']}/{payload['level']}]: {payload['message']}")

    tee = asyncio.create_task(tee_logs())

    stop_counter = CountingVerb(controller.device.stop)
    start_counter = CountingVerb(controller.device.start)
    controller.device.stop = stop_counter  # type: ignore[method-assign]
    controller.device.start = start_counter  # type: ignore[method-assign]

    failures: list[str] = []

    def check(phase: str, condition: bool, what: str) -> None:
        log(f"  {'PASS' if condition else 'FAIL'}: {phase}: {what}")
        if not condition:
            failures.append(f"{phase}: {what}")

    async def finish() -> int:
        EVIDENCE["failures"] = failures
        EVIDENCE["logTally"] = {
            "drops": tally.drops,
            "reason531": tally.reason531,
            "reconnects": tally.reconnects,
            "boots": tally.boots,
            "overflows": tally.overflows,
            "rst0xc": tally.rst0xc,
        }
        if meter.summary is not None or meter.inputs:
            EVIDENCE["meter"] = {"summary": meter.summary, "inputs": meter.inputs[-8:]}
        EVIDENCE["logs"] = controller.logs()
        if args.out is not None:
            args.out.write_text(json.dumps(EVIDENCE, indent=2, ensure_ascii=False))
        print("\n==== summary ====")
        print(json.dumps({"failures": failures, "phases": EVIDENCE["phases"],
                          "logTally": EVIDENCE["logTally"]}, indent=2, ensure_ascii=False))
        tee.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tee
        await controller.stop()
        tmp.cleanup()
        return 1 if failures else 0

    # The first HELLO is always lost (the open pulses RTS→EN and the board is in
    # its boot window) — the connect loop's backoff is the specified mechanism.
    if not await wait_until(
        lambda: controller.snapshot()["control"]["link"] == "UP", 30.0, "the control link"
    ):
        failures.append("attach: the control link never came up")
        return await finish()
    attached = record("A1-attached", controller)
    check("A1-attached", attached["mode"] == "IDLE", "the board starts IDLE")

    # ── half A: the mid-press drop ────────────────────────────────────────────

    if not args.skip_mid_press:
        log("PHASE A2: wake the console now — the script waits for the console link.")
        if not await wait_until(
            lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
            PHASE_TIMEOUT,
            "the console link to connect",
        ):
            failures.append("A2-console: the console never connected")
            return await finish()
        record("A2-console", controller)

        log("PHASE A3: uploading the mid-press macro (A held 60 s of every 60 s loop) and starting.")
        if not await user_starts_a_run(controller, "mid_press.json"):
            failures.append("A3-running: the mid-press run could not be started")
            return await finish()
        # Prove the fixture is what this half assumes: frame 0 is the press.
        entry = controller.macros.resolve("mid_press.json")
        frames = list(iter_frames(entry.plan)) if entry and entry.plan else []
        frame0_press = bool(frames) and frames[0][0][:3] != (0, 0, 0)
        check("A3-running", frame0_press, "frame 0 of the fixture is a button press")
        record("A3-running", controller, {"frame0Press": frame0_press, "frames": len(frames)})
        if not await wait_until(
            lambda: controller.snapshot()["mode"] == "MACRO"
            and controller.snapshot()["console"]["link"] == "CONNECTED",
            10.0,
            "the run to be going with the console up",
        ):
            failures.append("A3-running: the run did not reach MACRO with the console connected")
            return await finish()

        log("PHASE A4: put the console to SLEEP now — the script waits for the drop.")
        # Track the last snapshot that had the run going and the console up: it
        # is the *condition* the drop happened in.
        last_running: dict | None = None
        deadline = time.monotonic() + PHASE_TIMEOUT
        while time.monotonic() < deadline:
            snapshot = controller.snapshot()
            if snapshot["mode"] == "MACRO" and snapshot["console"]["link"] == "CONNECTED":
                last_running = snapshot
            if snapshot["console"]["link"] != "CONNECTED":
                break
            await asyncio.sleep(0.05)
        if last_running is None:
            failures.append("A4-drop: the console never dropped while the run was in MACRO")
            return await finish()
        dropped_frame = (last_running["plan"] or {}).get("currentFrame")
        dropped_loops = (last_running["plan"] or {}).get("loopCount")
        log(f"  the drop landed on frame {dropped_frame}, loop {dropped_loops}")

        # The stop is the policy's whole point: wait until it lands.
        await wait_until(lambda: controller.snapshot()["mode"] == "IDLE", 10.0, "the run to stop")
        stopped = record(
            "A5-stopped", controller, {"stopCalls": stop_counter.calls, "droppedFrame": dropped_frame}
        )
        check("A5-stopped", stopped["mode"] == "IDLE", "the run stopped on the drop")
        check("A5-stopped", stop_counter.calls == 1, f"exactly one STOP was sent ({stop_counter.calls})")
        check("A5-stopped", stopped["stopReason"] == "CONSOLE_LOST", "the UI reading is CONSOLE_LOST")
        check("A5-stopped", stopped["lastError"] is None, "no error is surfaced")
        check("A5-stopped", stopped["planHash"] is not None, "the plan is retained (one START away)")

        status = await controller.device.status()
        check(
            "A5-wire",
            status.last_stop_reason.name == "CONTAINER_STOP",
            f"the device's last_stop_reason is CONTAINER_STOP (saw {status.last_stop_reason.name})",
        )

        # The run's readout — the device's own account of what was on the wire.
        if not await wait_until(lambda: meter.summary is not None, 10.0, "the meter readout"):
            failures.append("A5-meter: the stop's meter readout never arrived")
        for reason in mid_press_failures(meter, dropped_frame):
            failures.append(f"A5-meter: {reason}")
        log(
            "  meter: "
            + json.dumps(
                {"summary": meter.summary, "lastInput": meter.last_input,
                 "neutralPending": meter.neutral_pending},
                ensure_ascii=False,
            )
        )
        record(
            "A5-meter",
            controller,
            {
                "meterSummary": meter.summary,
                "lastInput": meter.last_input,
                "carriedAPress": meter.carried_a_press,
                "neutralPending": meter.neutral_pending,
            },
        )

        log("PHASE A6: wake the console again — the script waits for the reconnect.")
        if not await wait_until(
            lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
            PHASE_TIMEOUT,
            "the console to reconnect",
        ):
            failures.append("A6-reconnect: the console never reconnected")
            return await finish()
        # Let the re-subscribe edge, the console's own re-init and any stray verb
        # land before reading the truth.
        await asyncio.sleep(5.0)
        woke = record("A6-reconnected", controller, {"startCalls": start_counter.calls})
        check("A6-reconnected", woke["mode"] == "IDLE", "the pass did NOT resume mid-press")
        check("A6-reconnected", start_counter.calls == 1, f"no second START was sent ({start_counter.calls})")
        check("A6-reconnected", woke["stopReason"] == "CONSOLE_LOST", "the drop story survives the wake")

    # ── half B: the release-build reboot rate ─────────────────────────────────

    if not args.skip_cycles:
        if args.skip_mid_press:
            log("PHASE B0: wake the console now — the cycle count needs a connected console.")
            if not await wait_until(
                lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
                PHASE_TIMEOUT,
                "the console link to connect",
            ):
                failures.append("B0-console: the console never connected")
                return await finish()
        base_drops, base_reason531 = tally.drops, tally.reason531
        base_boots, base_overflows, base_rst0xc = tally.boots, tally.overflows, tally.rst0xc
        log(
            f"PHASE B1: sleep/wake the console {args.cycles} times — "
            "one drop per sleep, one reconnect per wake."
        )
        started = time.monotonic()
        last_progress = 0.0
        while tally.drops - base_drops < args.cycles:
            done = tally.drops - base_drops
            if done != last_progress:
                last_progress = done
                log(
                    f"  cycle {done}/{args.cycles} "
                    f"(drops={tally.drops} reconnects={tally.reconnects} "
                    f"boots={tally.boots} overflows={tally.overflows})"
                )
            if time.monotonic() - started > PHASE_TIMEOUT * 2:
                log("  the cycle window closed before the target was reached")
                break
            await asyncio.sleep(0.2)
        cycles = tally.drops - base_drops
        console_ended = tally.reason531 - base_reason531
        reboots = tally.boots - base_boots
        overflows = tally.overflows - base_overflows
        rst0xc = tally.rst0xc - base_rst0xc
        rate = (reboots / cycles) if cycles else None
        record(
            "B2-rate",
            controller,
            {
                "cycles": cycles,
                "consoleEnded": console_ended,
                "reboots": reboots,
                "overflows": overflows,
                "rst0xc": rst0xc,
                "rebootRate": rate,
                "target": args.cycles,
            },
        )
        check("B2-rate", cycles >= args.cycles, f"the session reached {args.cycles} sleep/wake drops ({cycles})")
        check(
            "B2-rate",
            console_ended == cycles,
            f"every drop was the console ending it, reason=531 ({console_ended}/{cycles})",
        )
        check("B2-rate", overflows == 0, f"no `Tmr Svc` stack overflow ({overflows})")
        check("B2-rate", reboots == 0, f"no reboot across {cycles} cycles ({reboots})")

    return await finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
