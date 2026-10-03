#!/usr/bin/env python3
"""Bench the console's NFC polling and the 540-byte read (issue #36).

§12.2 validations 4 and 5 — the design's largest single risk, and the gate the
feasibility branch hangs on. One session, three observations, one console:

**Half A — validation 4a: does moving the NFC state byte alone start anything?**
A tag is placed while the console is connected but sits on no scan screen. The
device moves report byte `0x0C` to `0x02` (`STATUS.tag_state` says `PLACED`), and
the bench watches every console NFC subcommand for `--baseline` seconds. The
earlier research says the console only ever probes `0x01/0x0C` in that state;
this half either confirms it or finds the console polling on its own.

**Half B — validation 4b: does the console poll a *served* tag?** The operator
opens the console's amiibo scan screen; the bench waits for the console to start
asking (`STATUS.console_polling` leaves `IDLE`), then places a freshly-sealed
identity through the container's real path (`place_figure`, §8.5). This is the
physical world's ordering — the reader asks, then the amiibo is held to it.

**Half C — validation 5: does the console read all 540 bytes, uncorrupted?** The
device's #36 trace drains at the scan's edge (`console nfc:` lines, one per
distinct subcommand/offset with the CRC of exactly the bytes served), and this
script reconstructs every `0x15` response from the image *it* placed and checks
the CRC — end to end through sealing, staging, placement and serving. The open
§6.6 question is answered from the console's own offsets: the read sequence is
reported against both the plain space (`wire == image`) and the capture's
hypothesis (`wire == image + 0x3C`, the `0x46`→`0x0A` exchange), and the verdict
names which space the console actually read.

G-12 rides along: the probe line records that the firmware answered `61 12 50 10`
(not the documented `0d`), and whether the console polled anyway.

**Human actions** (the script prints one as it reaches each, and waits):

  C1  wake the console so the link connects                          (≤240 s)
  B1  leave the console on its home menu — NO scan screen            (--baseline s)
  S1  open System Settings → amiibo → scan, and keep it open         (≤240 s)
  S2  what the console's screen showed once the scan ended           (prompted)

A failure is a result: every verdict is recorded (in `--out` evidence JSON and
the summary) whether the console read everything, read garbage, or never asked.

Usage:
    container/.venv/bin/python scripts/bench_amiibo_read.py
    container/.venv/bin/python scripts/bench_amiibo_read.py --skip-baseline \
        --figure '~/clone/Amiibo/Amiibo Bin/Zelda/Link.bin' --out /tmp/row.json
    container/.venv/bin/python scripts/bench_amiibo_read.py --scans 2   # one rotation
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PHASE_TIMEOUT = 240.0
EVIDENCE: dict = {"phases": []}

#: §6.6's two candidate offset spaces, evaluated side by side. The capture's one
#: read exchange is wire `0x46` for image `0x0A`, i.e. a constant shift of
#: `0x3C`; the spec's default until this bench says otherwise is the plain space.
WIRE_BASE_PLAIN = 0
WIRE_BASE_CAPTURE = 0x3C

#: §2.2's CRC over what the device served — the same call the trace stamps.
_CRC_POLY = 0x1021


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC_POLY) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


# ── the device log, parsed the way the Logs screen renders it ────────────────

#: `console nfc: t=4331 sub=15 off=0046 n=67 crc=e5f6 reps=1` — one exchange.
_EVENT = re.compile(
    r"^console nfc: t=(?P<t>\d+) sub=(?P<sub>[0-9a-f]{2})"
    r"(?: off=(?P<off>[0-9a-f]{4}))?"
    r"(?: want=(?P<want>\d+))?"
    r"(?: got=(?P<got>\d+))?"
    r"(?: status=(?P<status>[0-9a-f]{2}))?"
    r"(?: n=(?P<n>\d+))?"
    r"(?: crc=(?P<crc>[0-9a-f]{4}))?"
    r"(?: reps=(?P<reps>\d+))?"
    r"(?: len=(?P<len>\d+))?"
    r"(?: cfg=(?P<cfg>[0-9a-f]*))?"
    r"(?: data=(?P<data>[0-9a-f]*))?$"
)

#: `console nfc: scan cmds=15 [03=1 04=1 05=13 06=1 14=1 15=9] reps=12 drops=0`.
_SUMMARY = re.compile(
    r"^console nfc: scan cmds=(?P<cmds>\d+) \[03=(?P<c03>\d+) 04=(?P<c04>\d+) "
    r"05=(?P<c05>\d+) 06=(?P<c06>\d+) 14=(?P<c14>\d+) 15=(?P<c15>\d+)\] "
    r"reps=(?P<reps>\d+) drops=(?P<drops>\d+)$"
)

#: `console nfc: sub=0c rsp=61125010` — the probe, logged inline by ns2_codec.
_PROBE = re.compile(r"^console nfc: sub=0c rsp=(?P<rsp>[0-9a-f]{8})$")

# `sub`, `off`, `status` and `crc` are hex on the wire; `t`, `n`, `want`,
# `got`, `reps` and `len` are decimal.
_HEX_FIELDS = {"sub": 16, "off": 16, "status": 16, "crc": 16}


def parse_nfc_line(message: str) -> dict | None:
    """One `console nfc:` line, or `None` for any other message.

    The container's log demux keeps the ESP-IDF tag (``control: console nfc: …``),
    so the match is on the ``console nfc:`` marker wherever it sits — §2.2's
    "log noise is what framing survives" cuts both ways.
    """
    if "console nfc:" not in message:
        return None
    message = "console nfc:" + message.split("console nfc:", 1)[1]
    match = _PROBE.search(message)
    if match is not None:
        return {"kind": "probe", "rsp": match.group("rsp")}
    match = _SUMMARY.search(message)
    if match is not None:
        out: dict = {"kind": "summary", "cmds": int(match.group("cmds"))}
        for name in ("c03", "c04", "c05", "c06", "c14", "c15", "reps", "drops"):
            out[name] = int(match.group(name))
        return out
    match = _EVENT.search(message)
    if match is None:
        return None
    event: dict = {"kind": "event"}
    for name, value in match.groupdict().items():
        if value is None:
            continue
        if name in ("cfg", "data"):
            event[name] = bytes.fromhex(value)
        elif name in _HEX_FIELDS:
            event[name] = int(value, _HEX_FIELDS[name])
        else:
            event[name] = int(value)
    return event


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ── the tally: what the console did, as the device saw it ────────────────────


@dataclass
class NfcTally:
    """Every `console nfc:` line, kept in arrival order.

    The device coalesces repeats (a scan screen held open asks `0x05` over and
    over), so `events` is distinct exchanges and each entry's `reps` says how
    often it recurred. The `0x0C` probe is counted here too — G-12's question is
    whether the console proceeds past it.
    """

    events: list[dict] = field(default_factory=list)
    summaries: list[dict] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)

    def feed(self, message: str) -> bool:
        parsed = parse_nfc_line(message)
        if parsed is None:
            return False
        if parsed["kind"] == "probe":
            self.probes.append(parsed["rsp"])
        elif parsed["kind"] == "summary":
            self.summaries.append(parsed)
        else:
            self.events.append(parsed)
        return True

    def events_after(self, index: int, subs: tuple[int, ...]) -> list[dict]:
        return [e for e in self.events[index:] if e.get("sub") in subs]

    def reads(self, index: int = 0) -> list[dict]:
        """The `0x15` exchanges from `index` on, oldest first."""
        return self.events_after(index, (0x15,))

    def writes(self, index: int = 0) -> list[dict]:
        return self.events_after(index, (0x14,))

    @property
    def polled(self) -> bool:
        """Validation 4's question: any NFC subcommand beyond the probe."""
        return any(e.get("sub") != 0x0C for e in self.events)


# ── validation 5: what the console asked for, and what it was served ─────────


def image_slice_for(image: bytes, off: int, n: int, base: int) -> bytes | None:
    """The response payload a `base`-mapped server builds for wire `off`.

    `00 · off LE · image[off-base : off-base+n-3]`, clamped to the image — the
    exact bytes `nfc_reply_read` assembles, so a CRC match says the bytes that
    left the device are the bytes the container placed, end to end.
    """
    chunk = n - 3
    if chunk <= 0 or off < base:
        return None
    start = off - base
    if start >= len(image):
        return None
    end = min(start + chunk, len(image))
    return bytes([0x00, off & 0xFF, (off >> 8) & 0xFF]) + image[start:end]


def verify_reads(
    reads: list[dict],
    image: bytes,
    writes: list[dict] | None = None,
    base: int = WIRE_BASE_PLAIN,
) -> dict:
    """CRC-check every read against `image`, applying `0x14` writes as they came.

    Reads and writes are walked **in the order the console sent them** (`t`),
    and a write is taken into the image at its wire offset, plain — exactly what
    the firmware does (§6.5) — so a read that preceded a write verifies against
    the bytes that were actually on the tag when the console read them, not
    against a copy already scribbled on later.
    """
    served = bytearray(image)
    events = sorted(
        list(reads) + list(writes or []),
        key=lambda e: e.get("t", 0),
    )
    checked = mismatched = unaddressable = 0
    for event in events:
        if event.get("sub") == 0x14:
            data = event.get("data")
            if data:
                start = event["off"]
                served[start : start + len(data)] = data[: max(0, len(served) - start)]
            continue
        r = event
        payload = image_slice_for(bytes(served), r["off"], r["n"], base)
        if payload is None:
            unaddressable += 1
            continue
        if len(payload) < r["n"]:
            mismatched += 1  # a short image cannot be what was served whole
            continue
        if crc16_ccitt(payload) == r["crc"]:
            checked += 1
        else:
            mismatched += 1
    return {"checked": checked, "mismatched": mismatched, "unaddressable": unaddressable}


def wire_coverage(reads: list[dict], base: int, image_size: int = 540) -> dict:
    """The image range the console's reads cover, under a `base` wire mapping.

    Two completeness notions, because the capture's offset space breaks the
    naive one: under `wire = image + 0x3C` a console that starts at wire `0x46`
    can never ask for image bytes `0x00..0x09` — they sit below its first
    readable address, and `0x05` supplied them as the UID. `complete` demands
    all 540; `completeFromFirst` demands every image byte from the first read's
    own address upward, which is the honest "it read everything it could have"
    under a shifted space.
    """
    span = bytearray(image_size)
    asked = 0
    first_image_off = None
    for r in reads:
        start = r["off"] - base
        if start < 0 or start >= image_size:
            continue
        if first_image_off is None or start < first_image_off:
            first_image_off = start
        for i in range(max(0, r["n"] - 3)):
            image_off = start + i
            if image_off < image_size and not span[image_off]:
                span[image_off] = 1
                asked += 1
    complete_from_first = False
    if first_image_off is not None:
        complete_from_first = all(span[first_image_off:])
    return {
        "base": base,
        "imageBytesCovered": asked,
        "of": image_size,
        "complete": asked == image_size,
        "firstImageOffset": first_image_off,
        "completeFromFirst": complete_from_first,
    }


def read_span_ms(reads: list[dict]) -> int | None:
    """First `0x15` to last, on the device's own clock."""
    if not reads:
        return None
    return reads[-1]["t"] - reads[0]["t"]


def offset_sequence(reads: list[dict]) -> list[int]:
    return [r["off"] for r in reads]


def round_trips(reads: list[dict]) -> int:
    """§6.4's arithmetic: 540 B at 64 B per exchange is nine round trips."""
    return sum(max(1, (r["n"] - 3 + 63) // 64) for r in reads)


# ── the phases ────────────────────────────────────────────────────────────────


def default_figure() -> Path | None:
    """The corpus's first `.bin`, deterministically.

    `docs/references.md` resolves `Amiibo` under `$REFERENCE_ROOT` (default
    `~/clone`); `!Essential Files` is the key, not a figure, and is excluded.
    """
    root = Path(os.environ.get("REFERENCE_ROOT", str(Path.home() / "clone"))) / "Amiibo"
    candidates = sorted(glob.glob(str(root / "Amiibo Bin" / "**" / "*.bin"), recursive=True))
    candidates = [c for c in candidates if "!Essential" not in c]
    return Path(candidates[0]) if candidates else None


def default_key() -> Path:
    """The retail key, in either spelling the corpus ships it (§6.7)."""
    root = Path(os.environ.get("REFERENCE_ROOT", str(Path.home() / "clone"))) / "Amiibo"
    for candidate in (root / "!Essential Files" / "key_retail.bin",
                      root / "Amiibo Bin" / "!Essential Files" / "key_retail.bin"):
        if candidate.is_file():
            return candidate
    return root / "!Essential Files" / "key_retail.bin"


def record(phase: str, controller, extra: dict | None = None) -> dict:
    snap = controller.snapshot()
    row = {
        "phase": phase,
        "wall": time.time(),
        "mode": snap["mode"],
        "consoleLink": snap["console"]["link"],
        "placement": snap.get("placement"),
    }
    if extra:
        row.update(extra)
    EVIDENCE["phases"].append(row)
    log(f"  {phase}: mode={row['mode']} console={row['consoleLink']} placement={row['placement']}")
    return row


async def wait_until(predicate, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.5)
    log(f"TIMEOUT waiting for {what} after {timeout:.0f}s")
    return False


class PlacementRecorder:
    """Wraps the device session's `place` so every sealed image is captured.

    The bench verifies served bytes against what the container *actually*
    placed — including the auto-rotation's placements after `SCAN_ENDED`
    (§6.5) — so the timeline of images is evidence, not an assumption.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.timeline: list[tuple[float, bytes]] = []

    async def __call__(self, tag: bytes, *, on_progress=None) -> None:
        self.timeline.append((time.monotonic(), bytes(tag)))
        await self.inner(tag, on_progress=on_progress)

    def image_at(self, when: float) -> bytes | None:
        current = None
        for t, image in self.timeline:
            if t <= when:
                current = image
            else:
                break
        return current


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--figure", default=None,
                        help="a figure `.bin` to serve (default: the corpus's first)")
    parser.add_argument("--key", default=None, help="key_retail.bin (default: the corpus's)")
    parser.add_argument("--baseline", type=float, default=60.0,
                        help="seconds to watch the byte move with no scan screen open")
    parser.add_argument("--scan-timeout", type=float, default=90.0,
                        help="seconds to watch for the read once the tag is placed")
    parser.add_argument("--scans", type=int, default=1,
                        help="scans to record (2 exercises the §6.5 rotation)")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--observed", default=None,
                        help="what the console's screen showed (recorded verbatim)")
    parser.add_argument("--out", type=Path, default=None, help="write the evidence JSON here")
    args = parser.parse_args()

    # Imported here, not at module scope: the parsing above is stdlib and the
    # offline test imports this module without aiohttp or pyserial.
    from container.ns2container import Settings, create_controller

    figure = Path(args.figure).expanduser() if args.figure else default_figure()
    key = (Path(args.key).expanduser() if args.key else default_key())
    if figure is None or not figure.is_file():
        log(f"FAIL: no figure to serve (looked for {figure!r}); pass --figure")
        return 1
    if not key.is_file():
        log(f"FAIL: no key at {key!r}; pass --key")
        return 1

    tmp = tempfile.TemporaryDirectory()
    macro_dir = Path(tmp.name) / "macros"
    amiibo_dir = Path(tmp.name) / "amiibo"
    macro_dir.mkdir()
    amiibo_dir.mkdir()
    shutil.copy(figure, amiibo_dir / figure.name)

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

    tally = NfcTally()
    placements = PlacementRecorder(controller.device.place)
    controller.device.place = placements  # type: ignore[method-assign]

    async def tee_logs() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for name, payload in controller.subscribe():
                if name == "log" and tally.feed(payload["message"]):
                    log(f"  ui-log [{payload['source']}/{payload['level']}]: "
                        f"{payload['message']}")

    tee = asyncio.create_task(tee_logs())

    failures: list[str] = []

    def check(phase: str, condition: bool, what: str) -> None:
        log(f"  {'PASS' if condition else 'FAIL'}: {phase}: {what}")
        if not condition:
            failures.append(f"{phase}: {what}")

    async def finish() -> int:
        EVIDENCE["failures"] = failures
        EVIDENCE["nfc"] = {
            "probes": tally.probes,
            "events": tally.events,
            "summaries": tally.summaries,
        }
        EVIDENCE["logs"] = controller.logs()
        if args.out is not None:
            args.out.write_text(json.dumps(EVIDENCE, indent=2, ensure_ascii=False))
        print("\n==== summary ====")
        print(json.dumps({"failures": failures, "phases": EVIDENCE["phases"],
                          "nfc": EVIDENCE["nfc"]}, indent=2, ensure_ascii=False))
        tee.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tee
        await controller.stop()
        tmp.cleanup()
        return 1 if failures else 0

    # ── attach ────────────────────────────────────────────────────────────────
    if not await wait_until(
        lambda: controller.snapshot()["control"]["link"] == "UP", 30.0, "the control link"
    ):
        failures.append("attach: the control link never came up")
        return await finish()
    snap = controller.snapshot()
    record("A1-attached", controller, {"firmware": snap["firmware"]})
    check("A1-attached", snap["firmware"]["features"]["amiibo"] is True,
          "the firmware reports the AMIIBO feature (§6.7's third lock)")
    check("A1-attached", snap["key"] == "KEY_OK",
          f"the mounted key is usable (state {snap['key']!r})")
    figure_id = snap["figures"][0]["id"]
    log(f"  figure: {snap['figures'][0]['name']} ({figure.name})")

    # ── C1: the console ───────────────────────────────────────────────────────
    log("PHASE C1: wake the console now — the script waits for the console link.")
    if not await wait_until(
        lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
        PHASE_TIMEOUT,
        "the console link to connect",
    ):
        failures.append("C1-console: the console never connected")
        return await finish()
    record("C1-console", controller)

    # ── half A: the byte moves, nobody asks ───────────────────────────────────
    if not args.skip_baseline:
        log(f"PHASE B1: leave the console on its HOME menu — no scan screen — "
            f"for {args.baseline:.0f}s.")
        await asyncio.sleep(2.0)
        before = len(tally.events)
        await controller.place_figure(figure_id)
        record("B1-placed", controller)
        await asyncio.sleep(args.baseline)
        polling = [e for e in tally.events[before:] if e.get("sub") != 0x0C]
        reads_like = [e for e in polling if e.get("sub") in (0x03, 0x06, 0x15)]
        snap = controller.snapshot()
        record("B1-observed", controller, {
            "nfcEvents": [e for e in tally.events[before:]],
            "tagPlaced": bool(snap.get("placement")),
        })
        check("B1-byte", bool(snap.get("placement")),
              "the tag is placed — the report byte is 0x02 (§4.9)")
        # The earlier research said "only ever 0x0C"; the run found the console
        # asking `0x05` on its own — a *result*, not a failure. What cannot
        # happen outside a scan context is a read, and that is the assertion.
        check("B1-no-read", not reads_like,
              f"no read subcommand (03/06/15) while no scan screen was open "
              f"(saw {[hex(e['sub']) for e in reads_like]}; status asks: "
              f"{sum(1 for e in polling if e.get('sub') == 0x05)})")
        EVIDENCE["baseline"] = {
            "seconds": args.baseline,
            "events": tally.events[before:],
            "polledBeyondProbe": bool(polling),
        }
        await controller.unplace()
        record("B1-unplaced", controller)

    # ── half B/C: the console asks, the tag is served, the read is checked ────
    scans_done = 0
    for scan_index in range(1, args.scans + 1):
        log(f"PHASE S1 (scan {scan_index}/{args.scans}): wake the console if it slept, open the "
            f"amiibo scan screen and KEEP IT OPEN — the script waits for the console link.")
        if not await wait_until(
            lambda: controller.snapshot()["console"]["link"] == "CONNECTED",
            PHASE_TIMEOUT,
            f"the console link (scan {scan_index})",
        ):
            failures.append(f"S{scan_index}: the console link never came up")
            break
        # `console_polling` is a *level* that survives a link drop (§3.2), so it
        # cannot by itself say "the console is asking now". The witness that
        # works is the container's scan count: `place_figure` zeroes its edge
        # detector, and the count rises exactly when a *placed* tag is asked
        # about (§3.2's up-edge). So: place first when nothing is placed, then
        # wait for the count.
        first_ask = len(tally.events)
        summaries_before = len(tally.summaries)
        if controller.snapshot().get("placement") is None:
            await controller.place_figure(figure_id)
        placed_at = time.monotonic()
        record(f"S{scan_index}-placed", controller)
        log("PHASE S1: the tag is served — open/navigate to the scan prompt on the console now.")

        def scan_complete(*, first_ask=first_ask, summaries_before=summaries_before) -> bool:
            # The scan's *end*, not its first ask: the console asks `0x05` on its
            # own with a tag placed (run 1's finding), so a rising scan count is
            # an ask, not a read. The witnesses that mean "done" are a traced
            # `0x15` or the trace's summary line — which only drains when the
            # console sends `0x04` (#36's deferred readout).
            return bool(tally.reads(first_ask)) or len(tally.summaries) > summaries_before

        await wait_until(scan_complete, args.scan_timeout,
                         f"the console to read (scan {scan_index})")
        await asyncio.sleep(3.0)  # the drain lands a tick after the console's 0x04

        # The backstop drain: an unplace owes the readout too, so whatever the
        # console did is on the log before the analysis reads it.
        snap = controller.snapshot()
        scans_done = max(scans_done, (snap.get("placement") or {}).get("scans", 0))
        await controller.unplace()
        # The backstop drain lands on the device's next 10 ms tick and reaches
        # this process through the log subscription — both after `unplace`
        # returns. Waiting here is what makes the drained lines part of the
        # analysis rather than a race the teardown loses (run 2's lesson).
        await asyncio.sleep(2.0)
        record(f"S{scan_index}-observed", controller, {"events": tally.events[first_ask:]})

        reads = tally.reads(first_ask)
        writes = tally.writes(first_ask)
        image = placements.image_at(placed_at) or b""
        analysis = {
            "roundTrips": round_trips(reads),
            "offsets": [f"0x{off:04x}" for off in offset_sequence(reads)],
            "spanMs": read_span_ms(reads),
            "writes": [{"off": f"0x{w['off']:04x}", "want": w.get("want"), "got": w.get("got")}
                       for w in writes],
            "events": tally.events[first_ask:],
            "spaces": {},
        }
        for name, base in (("plain", WIRE_BASE_PLAIN), ("capture", WIRE_BASE_CAPTURE)):
            analysis["spaces"][name] = {
                "wireBase": base,
                "coverage": wire_coverage(reads, base),
                "crc": verify_reads(reads, image, writes, base),
            }
        EVIDENCE.setdefault("scans", []).append({
            "index": scan_index,
            "identity": (snap.get("placement") or {}).get("identity"),
            "imageSha256": hashlib.sha256(image).hexdigest()[:16] if image else None,
            "analysis": analysis,
        })
        log(f"  scan {scan_index}: {analysis['roundTrips']} round trips at "
            f"{analysis['offsets'][:12]}{'…' if len(analysis['offsets']) > 12 else ''}")
        for name, space in analysis["spaces"].items():
            log(f"    {name} space (base 0x{space['wireBase']:02x}): "
                f"coverage {space['coverage']['imageBytesCovered']}/{space['coverage']['of']}, "
                f"crc checked={space['crc']['checked']} mismatched={space['crc']['mismatched']} "
                f"unaddressable={space['crc']['unaddressable']}")

    # ── the operator's observation ────────────────────────────────────────────
    if args.observed is None and sys.stdin.isatty():
        try:
            args.observed = input("PHASE S2: what did the console's screen show? ").strip()
        except EOFError:
            args.observed = None
    EVIDENCE["observed"] = args.observed
    log(f"operator observation: {args.observed!r}")

    # ── the verdicts ──────────────────────────────────────────────────────────
    all_reads = [e for e in tally.events if e.get("sub") == 0x15]
    verdict = {
        "validation4_consolePolled": tally.polled,
        "validation4_probeOnly": not tally.polled and bool(tally.probes),
        "g12_probeRsp": tally.probes[-1] if tally.probes else None,
        "g12_probesSeen": len(tally.probes),
        "validation5_reads": len(all_reads),
    }
    if EVIDENCE.get("scans"):
        spaces = EVIDENCE["scans"][0]["analysis"]["spaces"]
        for name in ("plain", "capture"):
            coverage = spaces[name]["coverage"]
            verdict[f"validation5_complete_{name}"] = (
                coverage["complete"] or coverage["completeFromFirst"])
            verdict[f"validation5_crcVerified_{name}"] = (
                spaces[name]["crc"]["checked"] > 0 and spaces[name]["crc"]["mismatched"] == 0)
    EVIDENCE["verdict"] = verdict
    print("\n==== verdict ====")
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    return await finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
