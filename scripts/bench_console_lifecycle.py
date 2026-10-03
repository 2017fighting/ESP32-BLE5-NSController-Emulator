#!/usr/bin/env python3
"""Bench the console-lifecycle policy on the real board (issue #31, §9.3).

Drives the real `Controller` — the same state projection the four screens
render — against the flashed board over the CH9102. The policy under test is
chapter 9's, decided container-side because the device watches neither link
(ADR-0008):

  * a console-link drop mid-macro stops the run (one `STOP`; the device sees a
    plain `CONTAINER_STOP`, the UI reads `CONSOLE_LOST`) and nothing restarts
    it when the console returns;
  * a device reboot mid-macro is normal operation (§9.2): the run is simply
    gone — `mode=IDLE`, `last_stop_reason=NONE`, no error surfaced.

The rotation half of the policy (a kept placement re-minted before the
console's next scan) is **not** benchable on this firmware: `HELLO.features`
reports `amiibo = false` until #25/#32 land, so no placement can exist to
rotate. The offline suite pins it; the bench records the deferral.

One human action per phase, printed as the script reaches it:

  phase 1  wake the console so the link connects (≤240 s)
  phase 3  sleep the console mid-run so the link drops (≤240 s)
  phase 5  wake the console again (≤240 s)

Phase 7 resets the board in place by pulsing RTS→EN from a second opener (the
node is non-exclusive on macOS, §3.2 of `control-link-container-seams.md`).

Usage:
    python3 scripts/bench_console_lifecycle.py [--port /dev/cu.usbmodemXXXX] \
        [--baud 115200]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from container.ns2container import ControllerError, Settings, create_controller  # noqa: E402
from container.ns2device import CommandError  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "plan" / "correction.json"
PHASE_TIMEOUT = 900.0
EVIDENCE: dict = {"phases": []}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class CountingVerb:
    """Wraps one device verb so 'sent once' is measured, not asserted."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.inner()


async def wait_until(predicate, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    log(f"TIMEOUT waiting for {what} after {timeout:.0f}s")
    return False


async def user_starts_a_run(controller, tries: int = 4) -> bool:
    """Drive `start_macro` the way §8.9 says a *human* may: never re-send a
    mode verb on a slow reply — read the truth, then decide.

    A console reconnecting mid-verb can drown the reply in the DEBUG log flood
    at 115200 (measured: START's reply lost past the 2 s request timeout). The
    container correctly marks the link down and surfaces the error; the user's
    recovery is to re-attach and look: if the mode already moved, the verb
    landed late and there is nothing to retry.
    """
    for attempt in range(1, tries + 1):
        try:
            await controller.start_macro("correction.json")
            return True
        except (ControllerError, CommandError) as failure:
            log(f"  start attempt {attempt} failed: {failure}")
            await wait_until(
                lambda: controller.snapshot()["control"]["link"] == "UP", 15.0, "the link to return"
            )
            await asyncio.sleep(1.0)
            if controller.snapshot()["mode"] == "MACRO":
                log("  the START landed late — the run is already in MACRO")
                return True
            log("  the run is not going; starting again as a user would")
    return controller.snapshot()["mode"] == "MACRO"


async def user_stops_the_run(controller) -> None:
    """Stop the run if one is going; tolerate the same drowned-reply failure."""
    if controller.snapshot()["mode"] == "IDLE":
        return
    for _ in range(3):
        try:
            await controller.stop_macro()
            return
        except (ControllerError, CommandError) as failure:
            log(f"  stop failed: {failure}")
            await wait_until(
                lambda: controller.snapshot()["control"]["link"] == "UP", 15.0, "the link to return"
            )
    with contextlib.suppress(Exception):
        await controller.refresh()


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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()

    tmp = tempfile.TemporaryDirectory()
    macro_dir = Path(tmp.name) / "macros"
    macro_dir.mkdir()
    shutil.copy(FIXTURE, macro_dir / "correction.json")
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

    # Every log line the Logs screen would show, in order.
    async def tee_logs() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for name, payload in controller.subscribe():
                if name == "log":
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
        EVIDENCE["logs"] = controller.logs()
        print("\n==== summary ====")
        print(json.dumps({"failures": failures, "phases": EVIDENCE["phases"]}, indent=2, ensure_ascii=False))
        tee.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tee
        await controller.stop()
        tmp.cleanup()
        return 1 if failures else 0

    # The first HELLO is always lost (the open pulses RTS→EN and the board is
    # in its boot window) — the connect loop's backoff is the specified
    # mechanism, so wait it out rather than papering over it.
    if not await wait_until(
        lambda: controller.snapshot()["control"]["link"] == "UP", 30.0, "the control link"
    ):
        return await finish()
    record("1-attached", controller)
    # A previous bench may have left a run looping into the void (§4.7's
    # accepted risk); a user at the screens would stop it first.
    if controller.snapshot()["mode"] == "MACRO":
        log("  a run is already going from an earlier session; stopping it first")
        await user_stops_the_run(controller)

    log("PHASE 1: wake the console now — the script waits for the console to connect.")
    if not await wait_until(
        lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
        PHASE_TIMEOUT,
        "the console link to connect",
    ):
        return await finish()

    log("PHASE 2: uploading the golden fixture and starting the run.")
    if not await user_starts_a_run(controller):
        check("2-running", False, "the run could not be started")
        return await finish()
    running = record("2-running", controller)
    check("2-running", running["mode"] == "MACRO", "the run is in MACRO")
    check("2-running", running["consoleLink"] == "CONNECTED", "the console is connected")

    log("PHASE 3: sleep the console now — the script waits for the link to drop.")
    if not await wait_until(
        lambda: controller.snapshot()["console"]["link"] != "CONNECTED",
        PHASE_TIMEOUT,
        "the console link to drop",
    ):
        return await finish()

    # The stop is the policy's whole point: wait until it lands.
    await wait_until(lambda: controller.snapshot()["mode"] == "IDLE", 10.0, "the run to stop")
    stopped = record("3-dropped", controller, {"stopCalls": stop_counter.calls})
    check("3-dropped", stopped["mode"] == "IDLE", "the run stopped on the drop")
    check("3-dropped", stop_counter.calls == 1, f"exactly one STOP was sent (sent {stop_counter.calls})")
    check("3-dropped", stopped["stopReason"] == "CONSOLE_LOST", "the UI reading is CONSOLE_LOST")
    check("3-dropped", stopped["lastError"] is None, "no error is surfaced")
    check("3-dropped", stopped["planHash"] is not None, "the plan is retained (one START away)")

    log("PHASE 4: reading the device's own stop reason (a plain CONTAINER_STOP).")
    status = await controller.device.status()
    check(
        "4-wire",
        status.last_stop_reason.name == "CONTAINER_STOP",
        f"the device's last_stop_reason is CONTAINER_STOP (saw {status.last_stop_reason.name})",
    )

    log("PHASE 5: wake the console again — the script waits for the reconnect.")
    if not await wait_until(
        lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
        PHASE_TIMEOUT,
        "the console to reconnect",
    ):
        return await finish()
    await asyncio.sleep(5.0)  # let the re-subscribe edge and any stray verb land
    woke = record("5-reconnected", controller, {"startCalls": start_counter.calls})
    check("5-reconnected", woke["mode"] == "IDLE", "no auto-restart after the wake")
    check("5-reconnected", start_counter.calls == 1, f"no second START was sent (sent {start_counter.calls})")
    check("5-reconnected", woke["stopReason"] == "CONSOLE_LOST", "the drop story survives the reconnect")

    log("PHASE 6: starting a second run to show a fresh run clears the drop reading.")
    if not await user_starts_a_run(controller):
        check("6-fresh-run", False, "the second run could not be started")
        return await finish()
    running2 = record("6-fresh-run", controller, {"startCalls": start_counter.calls})
    check("6-fresh-run", running2["mode"] == "MACRO", "the second run is in MACRO")
    # §3.4: a START does not clear `last_stop_reason` (the firmware keeps it
    # until the next stop), so the reading is asserted where the UI renders it
    # — in IDLE, after this run ends by hand.
    await user_stops_the_run(controller)
    fresh = record("6-stopped-by-hand", controller)
    check("6-stopped-by-hand", fresh["mode"] == "IDLE", "the second run stopped")
    check("6-stopped-by-hand", fresh["stopReason"] == "CONTAINER_STOP", "the fresh run's story replaces the drop's")

    log("PHASE 7: rebooting the board mid-run with an RTS pulse from a second opener.")
    if not await user_starts_a_run(controller):
        check("7-rebooted", False, "the third run could not be started")
        return await finish()
    record("7-running", controller)
    import serial

    # esptool's hard reset: EN is pulled low by DTR low + RTS high on the
    # CH9102's auto-reset circuit — DTR high throughout (as a naive open
    # leaves it) drives BOOT instead and resets nothing.
    pulse = serial.Serial()
    pulse.port = args.port
    pulse.baudrate = args.baud
    pulse.timeout = 0.1
    pulse.dtr = False
    pulse.rts = True
    pulse.open()
    await asyncio.sleep(0.2)
    pulse.setRTS(False)
    pulse.close()
    if not await wait_until(
        lambda: controller.snapshot()["recovery"] == "NEW_POWER", 30.0, "the NEW_POWER branch"
    ):
        return await finish()
    await asyncio.sleep(2.0)
    rebooted = record("7-rebooted", controller)
    check("7-rebooted", rebooted["mode"] == "IDLE", "the run is simply gone")
    check("7-rebooted", rebooted["stopReason"] == "NONE", "no stop reason is invented (§9.2)")
    check("7-rebooted", rebooted["lastError"] is None, "a reboot is not an error")
    check("7-rebooted", rebooted["planHash"] is None, "the plan is cleared; re-upload required")

    return await finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
