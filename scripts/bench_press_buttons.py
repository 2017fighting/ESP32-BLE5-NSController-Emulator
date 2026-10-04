#!/usr/bin/env python3
"""Drive one press sequence on the device's controller — the #36/#39 game tool.

The one experiment #36 could not run needs this device to be **player 1** (the
console's "press L+R on the controller you want to use" prompt), and once it is,
the operator's own pad no longer controls the game — so the bench drives the
inputs. This script compiles a one-shot press sequence into a plan, runs it once
and stops (the stop commits the neutral, §4.6), leaving the console to react.

The operator is the eyes: run one sequence, read the screen, run the next.
The one-shot sequence mode never places a tag — placement is the resident mode's
`place`/`place-same` (or `bench_amiibo_read.py`'s) — so the placement window
stays strict.

Item grammar: `item[:hold_ms[:gap_ms]]`, comma-separated, where an item is
either `button[+button…]` or a stick `rstick:H,V` / `lstick:H,V` (axes −1..1).
`hold` is how long the item stays down (or deflected); `gap` is how long to wait
after it before the next item — a game ignores input while its menu animation
closes, and a controller *change* is worse: the console re-initialises its input
pipeline (§9.1) and drops everything for the first moments (runs 6/7: L+R
landed, the A 230 ms later never did).

Usage:
    container/.venv/bin/python scripts/bench_press_buttons.py --seq "l+r:150"
    container/.venv/bin/python scripts/bench_press_buttons.py --seq "a:200,b:300:800"
    container/.venv/bin/python scripts/bench_press_buttons.py --seq "up:150:600,rstick:1/0:900:400,a:150:400,l:250" --dry-run

**The resident mode is the operator-coached bench** (`--fifo PATH`): driving a
game's menus and placing the tag cannot be separate processes — one holder of
the port (§10.5) — so the one process stays attached and reads commands from
the FIFO, one per line:

    press <seq>     run a press sequence (the grammar above)
    place           seal a fresh identity of --figure and place it
    place-same      re-place the figure under the identity already placed — the
                    console's "present the same amiibo again" placement (the
                    reference's continue-for-write, §6.6)
    unplace         take the tag off the field
    status          one snapshot line
    sleep <s>       pause (pacing/watching)
    quit            release the port and exit

The device's `console nfc:` lines are teed to stdout as they arrive, so the
operator-coached scan is watched from the same stream.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The default gap between items, and the neutral tail that closes the loop —
#: long enough that a console sampling at 60 Hz sees every state, short enough
#: that the whole sequence is one human-sized gesture.
GAP_MS = 80.0
TAIL_MS = 200.0

KNOWN_BUTTONS = (
    "a", "b", "x", "y", "l", "r", "zl", "zr", "plus", "minus",
    "up", "down", "left", "right", "home", "capture", "l_stick", "r_stick",
)

#: A stick item is `rstick:H/V` or `lstick:H/V` (axes in −1..1, `/`-separated
#: because `,` separates items). BotW's rune list is the reason this exists:
#: D-pad Up opens it, the **right stick** moves the selection (the documented
#: path — Nintendo Support/GameWith), and the amiibo icon sits **last**, so
#: `rstick:1/0` held to the end lands on it.
STICK_ITEMS = {"lstick": "left", "rstick": "right"}


def _parse_item(text: str) -> tuple:
    """One item: ("buttons", [names], hold, gap) or ("stick", side, h, v, hold, gap)."""
    fields = text.split(":")
    name = fields[0]
    if name in STICK_ITEMS:
        h_text, _, v_text = (fields[1] if len(fields) > 1 else "0").partition("/")
        hold = float(fields[2]) if len(fields) > 2 and fields[2] else 100.0
        gap = float(fields[3]) if len(fields) > 3 and fields[3] else GAP_MS
        h = float(h_text) if h_text else 0.0
        v = float(v_text) if v_text else 0.0
        return ("stick", STICK_ITEMS[name], h, v, hold, gap)

    hold = float(fields[1]) if len(fields) > 1 and fields[1] else 100.0
    gap = float(fields[2]) if len(fields) > 2 and fields[2] else GAP_MS
    buttons = [b.strip().lower() for b in name.split("+") if b.strip()]
    if not buttons:
        raise ValueError(f"empty item in {text!r}")
    for button in buttons:
        if button not in KNOWN_BUTTONS:
            raise ValueError(
                f"unknown button {button!r} (known: {', '.join(KNOWN_BUTTONS)}; "
                "sticks: rstick:H/V / lstick:H/V)")
    return ("buttons", buttons, hold, gap)


def parse_seq(spec: str) -> list[tuple]:
    """`l+r:150` → [('buttons', ['l','r'], 150.0, GAP_MS)], gaps overridable."""
    items = [_parse_item(part.strip()) for part in spec.split(",") if part.strip()]
    if not items:
        raise ValueError("no items in sequence")
    return items


def build_macro(spec: str) -> tuple[list[dict], float]:
    """The press sequence as the compiler's canonical input (§5.1), and its length."""
    events: list[dict] = []
    t = 0.0
    for item in parse_seq(spec):
        if item[0] == "stick":
            _, side, h, v, hold, gap = item
            events.append({"t": t, "ev": {"type": "stick", "stick": side, "h": h, "v": v}})
            t += hold
            events.append({"t": t, "ev": {"type": "stick", "stick": side, "h": 0.0, "v": 0.0}})
        else:
            _, buttons, hold, gap = item
            for button in buttons:
                events.append({"t": t, "ev": {"type": "button", "name": button, "pressed": True}})
            t += hold
            for button in buttons:
                events.append({"t": t, "ev": {"type": "button", "name": button, "pressed": False}})
        t += gap
    t += TAIL_MS
    return events, t


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seq", default=None, help='e.g. "l+r:150,a:100"')
    parser.add_argument("--fifo", default=None,
                        help="resident mode: read commands from this FIFO path")
    parser.add_argument("--figure", default=None,
                        help="resident mode's `place` figure (default: the corpus's first)")
    parser.add_argument("--key", default=None, help="key_retail.bin (default: the corpus's)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compile offline and print the plan; never touch a device")
    parser.add_argument("--tee-all", action="store_true",
                        help="tee every device log line, not just the NFC ones")
    args = parser.parse_args()

    if args.seq is None and args.fifo is None:
        parser.error("one of --seq or --fifo is required")

    from container.ns2plan import compile_macro

    if args.dry_run:
        events, loop_ms = build_macro(args.seq)
        plan = compile_macro(events)
        print(json.dumps({"events": events, "loopMs": loop_ms,
                          "records": plan.record_count},
                         indent=2))
        return 0

    import glob as _glob
    import os as _os
    import shutil as _shutil

    from container.ns2container import Settings, create_controller

    def default_figure() -> Path | None:
        root = Path(_os.environ.get("REFERENCE_ROOT", str(Path.home() / "clone"))) / "Amiibo"
        candidates = sorted(_glob.glob(str(root / "Amiibo Bin" / "**" / "*.bin"), recursive=True))
        candidates = [c for c in candidates if "!Essential" not in c]
        return Path(candidates[0]) if candidates else None

    def default_key() -> Path:
        root = Path(_os.environ.get("REFERENCE_ROOT", str(Path.home() / "clone"))) / "Amiibo"
        for candidate in (root / "!Essential Files" / "key_retail.bin",
                          root / "Amiibo Bin" / "!Essential Files" / "key_retail.bin"):
            if candidate.is_file():
                return candidate
        return root / "!Essential Files" / "key_retail.bin"

    tmp = tempfile.TemporaryDirectory()
    macro_dir = Path(tmp.name) / "macros"
    amiibo_dir = Path(tmp.name) / "amiibo"
    macro_dir.mkdir()
    amiibo_dir.mkdir()

    figure = (Path(args.figure).expanduser() if args.figure else default_figure())
    key = (Path(args.key).expanduser() if args.key else default_key())
    if args.fifo and (figure is None or not figure.is_file() or not key.is_file()):
        log(f"FAIL: resident mode needs a figure and the key (figure={figure!r} key={key!r})")
        return 1
    if figure is not None and figure.is_file():
        _shutil.copy(figure, amiibo_dir / figure.name)

    settings = Settings(
        port=args.port,
        baud=args.baud,
        macro_dir=macro_dir,
        amiibo_dir=amiibo_dir,
        key_file=key,
        key_dir=key.parent,
        static_dir=Path(tmp.name) / "dist",
    )
    controller = create_controller(settings)
    await controller.start()

    async def tee_logs() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for name, payload in controller.subscribe():
                # The stream also yields ("state", …) events, which carry no
                # "message" key — the name is checked before the key is read.
                if name == "log" and (
                    args.tee_all
                    or "console nfc:" in payload["message"]
                    or "nfc byte:" in payload["message"]
                ):
                    log(f"  device: {payload['message']}")

    tee = asyncio.create_task(tee_logs())

    async def attach() -> bool:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if controller.snapshot()["control"]["link"] == "UP":
                return True
            await asyncio.sleep(0.5)
        return False

    async def run_seq(spec: str) -> None:
        events, length_ms = build_macro(spec)
        (macro_dir / "press.json").write_text(json.dumps(events))
        await controller.rescan()
        await controller.start_macro("press.json")
        await asyncio.sleep(length_ms / 1000.0 + 0.35)
        await controller.stop_macro()
        log(f"  pressed {spec!r} ({length_ms:.0f} ms)")

    try:
        if not await attach():
            log("FAIL: the control link never came up")
            return 1

        if args.seq is not None:
            await run_seq(args.seq)
            snap = controller.snapshot()
            log(f"done: mode={snap['mode']} console={snap['console']['link']}")
            return 0

        # ── resident mode: one attach, commands from the FIFO ──────────────
        # Rotation off, deliberately: the operator owns placement timing here.
        # The §6.5 auto-rotation re-places on every SCAN_ENDED, and its 20 ms
        # tag-absent gap lands mid-retry when a console cycles its read — the
        # bench saw `0x05 status=00` exactly there (#39's session).
        async def _no_rotation() -> None:
            log("  (scan ended — rotation is off in resident mode)")

        controller._on_scan_ended = _no_rotation  # type: ignore[method-assign]
        fifo_path = Path(args.fifo)
        if fifo_path.exists():
            fifo_path.unlink()
        _os.mkfifo(fifo_path)
        log(f"resident: commands on {fifo_path} (press/place/unplace/status/sleep/quit)")
        figures = controller.snapshot().get("figures") or []
        figure_id = figures[0]["id"] if figures else None
        log(f"resident: figure {figure_id!r}, console={controller.snapshot()['console']['link']}")
        # One batch per writer: a FIFO read blocks until a writer arrives, and
        # each `echo cmd > fifo` is one writer, one batch — so the blocking read
        # lives in a thread and the event loop (the log tee, the 2 Hz poll)
        # keeps running while it waits.
        def read_batch() -> list[str]:
            with open(fifo_path, "r", encoding="utf-8") as commands:
                return commands.readlines()

        while True:
            for line in await asyncio.to_thread(read_batch):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                cmd = parts[0].lower()
                rest = parts[1] if len(parts) > 1 else ""
                log(f"cmd: {line}")
                try:
                    if cmd == "quit":
                        log("resident: bye")
                        return 0
                    if cmd == "press":
                        await run_seq(rest)
                    elif cmd == "place":
                        await controller.place_figure(figure_id)
                        log(f"  placed: {controller.snapshot().get('placement')}")
                    elif cmd == "place-same":
                        placed = controller.snapshot().get("placement") or {}
                        identity = placed.get("identity")
                        if not identity:
                            log("  place-same: nothing placed yet")
                        else:
                            await controller.place_figure(
                                figure_id, identity=bytes.fromhex(identity)
                            )
                            log(f"  re-placed same identity: {controller.snapshot().get('placement')}")
                    elif cmd == "unplace":
                        await controller.unplace()
                        log("  unplaced")
                    elif cmd == "status":
                        snap = controller.snapshot()
                        log(f"  mode={snap['mode']} console={snap['console']['link']} "
                            f"placement={snap.get('placement')}")
                    elif cmd == "sleep":
                        await asyncio.sleep(float(rest) if rest else 1.0)
                        log("  slept")
                    else:
                        log(f"  unknown command {cmd!r}")
                except Exception as failure:  # noqa: BLE001 — keep the resident alive
                    log(f"  command failed: {failure}")
                await asyncio.sleep(0.05)
    finally:
        tee.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tee
        await controller.stop()
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
