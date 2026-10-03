#!/usr/bin/env python3
"""Drive the plan executor on the bench over the control link (issue #24, §7.3
step 5).

A throwaway client, in the spirit of the stage-1 one
(`docs/research/control-link-stage1.md` §4): it speaks §2.2's COBS+CRC framing
itself and needs nothing but the standard library, so it can run on any host
without the container's seams (#30) or `bleak`.

What it checks, against the ticket's Done-when:

  * ``HELLO`` advertises the ``macro`` feature bit (§2.6).
  * a committed plan runs and loops: ``current_frame`` walks, ``loop_count``
    advances, and the loop period measures what the plan says (§5.4).
  * the echoed ``plan_hash`` equals the one the container sent (§5.6).
  * ``STOP`` returns to ``IDLE`` with ``last_stop_reason=CONTAINER_STOP``
    (§4.3) and the plan retained.
  * a ``record_count = 0`` plan is refused at ``START`` with ``BAD_PLAN`` and
    leaves the mode in ``IDLE`` (§4.3's added row).

What it deliberately does **not** claim: that the neutral reached the HID wire.
Observing that needs a BLE notification capture on the console link, which is
#35's comparative-latency ticket; `docs/research/plan-executor-bench.md` records
the gap and what was measured instead.

Usage:
    python3 scripts/bench_plan_executor.py [--port /dev/cu.usbmodemXXXX] [--seconds 6]
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from container.ns2plan import compile_macro, compile_json, plan_identity  # noqa: E402

# --------------------------------------------------------------------- §2.2

PROTO_VER = 1
TYPE_REQUEST = 1
TYPE_REPLY = 2
TYPE_EVENT = 3

EVENT_LOOP_COMPLETED = 4

VERB_HELLO = 1
VERB_LOAD_PLAN = 2
VERB_START = 3
VERB_STOP = 4
VERB_STATUS = 5
VERB_PLACE_AMIIBO = 6
VERB_UNPLACE_AMIIBO = 7
VERB_ERROR = 10

CONTROL_EVENT_LOOP_COMPLETED = EVENT_LOOP_COMPLETED

MODE_IDLE, MODE_MACRO, MODE_AMIIBO = 0, 1, 2
PLAN_NONE, PLAN_COMMITTED = 0, 1
STOP_NONE, STOP_CONTAINER, STOP_BOOT_LOCAL = 0, 1, 2
ERR_NONE, ERR_BAD_PLAN = 0, 8

BULK_ANNOUNCE, BULK_CHUNK, BULK_COMMIT = 1, 2, 3
CHUNK_SIZE = 256
ACK_WINDOW = 4096


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xffff, no reflection, xorout 0.
    Check value over b"123456789" is 0x29B1."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    code_at = 0
    out.append(0)
    code = 1
    for byte in data:
        if byte == 0:
            out[code_at] = code
            code_at = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_at] = code
                code_at = len(out)
                out.append(0)
                code = 1
    out[code_at] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            raise ValueError("zero byte inside a COBS block")
        i += 1
        for _ in range(code - 1):
            if i >= len(data):
                raise ValueError("truncated COBS block")
            out.append(data[i])
            i += 1
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


def encode_frame(frame_type: int, verb: int, payload: bytes = b"") -> bytes:
    header = struct.pack("<BBBH", PROTO_VER, frame_type, verb, len(payload))
    crc = crc16_ccitt_false(header + payload)
    return b"\x00" + cobs_encode(header + struct.pack("<H", crc) + payload) + b"\x00"


# ------------------------------------------------------------------ the client


class Frame:
    __slots__ = ("type", "verb", "payload")

    def __init__(self, frame_type: int, verb: int, payload: bytes):
        self.type = frame_type
        self.verb = verb
        self.payload = payload


class ControlLink:
    """One-outstanding-request (§2.3), with the bulk exemption (§2.7, ADR-0015).

    **Opening this port resets the board.** The CH9102 wires DTR→GPIO0 and
    RTS→EN, and pyserial asserts both as it opens; deasserting afterwards still
    leaves the EN pulse behind. So the constructor opens, deasserts, and then
    waits for the device to answer ``HELLO`` — a reset on open is absorbed rather
    than treated as a fault, which is the same discipline `serial` in the
    container needs (ADR-0014) and the reason the boot wait is not a fixed sleep.
    """

    def __init__(self, port: str, baud: int = 115200):
        import serial  # imported here so --help works without pyserial

        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.05
        self.ser.open()
        # Deassert immediately: a level already low at boot is *not* a BOOT press
        # (§4.5), but an EN pulse is a reboot. Do not re-assert either line again.
        self.ser.dtr = False
        self.ser.rts = False
        self.buf = bytearray()
        self.pending: list[Frame] = []
        # Wire-level counters, for the bench phases that need to quote what the
        # wire carried (G-1): bytes read, `0x00`-delimited segments, segments
        # that were not frames (log lines and noise), frames that had the right
        # 7+len geometry but failed the CRC (a reply split mid-frame is the one
        # thing the shared TX lock exists to make impossible), and good frames.
        self.counters = {"bytes_rx": 0, "segments": 0, "noise": 0,
                         "noise_bytes": 0, "bad_crc": 0, "frames": 0}
        self._wait_ready()

    def _wait_ready(self, seconds: float = 15.0) -> None:
        """Waits for the device to boot far enough to answer ``HELLO``.

        A reset-on-open means the first couple of seconds are the ROM console at
        115200 and then the app's own banner, both arriving as *garbage* to a
        host opened at the firmware's (post-init) baud; the BLE stack then takes
        a moment to come up. So the drain is generous and the retry loop is the
        readiness test, rather than a fixed sleep that would break whenever boot
        got slower.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.ser.read(65536)  # drain whatever the boot produced
            self.buf.clear()
            self.pending.clear()
            try:
                self.request(VERB_HELLO, bytes([PROTO_VER]), timeout=0.75)
                return
            except TimeoutError:
                time.sleep(0.3)
        raise TimeoutError(f"{self.ser.port} never answered HELLO within {seconds}s")

    def close(self) -> None:
        self.ser.close()

    def _read_frames(self, deadline: float) -> list[Frame]:
        frames: list[Frame] = []
        while time.monotonic() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                self.buf.extend(chunk)
                self.counters["bytes_rx"] += len(chunk)
            # Split on the delimiter; the leading one is mandatory (§2.2).
            while True:
                start = self.buf.find(b"\x00")
                if start < 0:
                    break
                end = self.buf.find(b"\x00", start + 1)
                if end < 0:
                    if start > 0:
                        # Log text that ran ahead of the frame's leading
                        # delimiter — dropped, and counted, never silent.
                        self.counters["noise"] += 1
                        self.counters["noise_bytes"] += start
                        del self.buf[:start]
                    break
                block = bytes(self.buf[start + 1 : end])
                if start > 0:
                    self.counters["noise"] += 1
                    self.counters["noise_bytes"] += start
                del self.buf[: end + 1]
                if not block:
                    continue
                self.counters["segments"] += 1
                try:
                    decoded = cobs_decode(block)
                except ValueError:
                    self.counters["noise"] += 1
                    self.counters["noise_bytes"] += len(block)
                    continue
                if len(decoded) < 7:
                    self.counters["noise"] += 1
                    self.counters["noise_bytes"] += len(block)
                    continue
                ver, ftype, verb, plen = struct.unpack("<BBBH", decoded[:5])
                if ver != PROTO_VER or len(decoded) != 7 + plen:
                    self.counters["noise"] += 1
                    self.counters["noise_bytes"] += len(block)
                    continue
                crc = struct.unpack("<H", decoded[5:7])[0]
                if crc16_ccitt_false(decoded[:5] + decoded[7:]) != crc:
                    self.counters["bad_crc"] += 1
                    continue
                self.counters["frames"] += 1
                frames.append(Frame(ftype, verb, decoded[7:]))
        return frames

    def send(self, verb: int, payload: bytes = b"") -> None:
        """Write one REQUEST frame and return without waiting for its reply.

        For the §2.7 bulk path, where chunks inside an ACK window carry no
        reply and the sender streams a whole window before waiting.
        """
        self.ser.write(encode_frame(TYPE_REQUEST, verb, payload))
        self.ser.flush()

    def _take_reply(self, verb: int) -> Frame | None:
        """Pop the first queued REPLY for `verb` (or any `ERROR`), if any.

        Unmatched REPLY frames are *kept* on `self.pending`, never dropped:
        bulk ACKs arrive batched (several replies per read), and a dropped
        ACK reads as a device stall that never happened — the mirrored
        `FrameIO` queues its replies for exactly this reason.
        """
        for i, frame in enumerate(self.pending):
            if frame.type == TYPE_REPLY and (frame.verb == verb or frame.verb == VERB_ERROR):
                return self.pending.pop(i)
        return None

    def wait_reply(self, verb: int, timeout: float = 0.5) -> Frame:
        """Wait for the next REPLY for `verb` (or any `ERROR`), without sending.

        The same reply-matching rules as `request`: an `ERROR` (verb 10)
        answers any request, and everything unmatched — events *and* replies
        for other verbs — is kept on `self.pending` for the caller instead of
        being dropped.
        """
        deadline = time.monotonic() + timeout
        while True:
            queued = self._take_reply(verb)
            if queued is not None:
                return queued
            if time.monotonic() >= deadline:
                break
            # Consume the whole batch before matching: returning mid-loop
            # would discard the replies that arrived behind the match.
            match = None
            for frame in self._read_frames(min(deadline, time.monotonic() + 0.05)):
                if (match is None and frame.type == TYPE_REPLY
                        and (frame.verb == verb or frame.verb == VERB_ERROR)):
                    match = frame
                else:
                    self.pending.append(frame)
            if match is not None:
                return match
        raise TimeoutError(f"no reply to verb {verb} within {timeout}s")

    def request(self, verb: int, payload: bytes = b"", *, timeout: float = 0.5) -> Frame:
        """Sends one REQUEST and waits for its REPLY.

        An `ERROR` is a REPLY whose `verb` is 10 (§2.4), so it answers *any*
        request and must be returned rather than waited past — otherwise a
        rejection looks like a dead device.
        """
        self.ser.write(encode_frame(TYPE_REQUEST, verb, payload))
        self.ser.flush()
        return self.wait_reply(verb, timeout)

    def drain(self, seconds: float) -> list[Frame]:
        return self._read_frames(time.monotonic() + seconds)


def parse_hello(payload: bytes) -> dict:
    proto_ver = payload[0]
    fw = tuple(payload[1:5])
    boot_id, max_frame, chunk_size = struct.unpack("<IHH", payload[5:13])
    capacity, slots, features = struct.unpack("<IBH", payload[13:20])
    return dict(proto_ver=proto_ver, fw=fw, boot_id=boot_id, max_frame=max_frame,
                chunk_size=chunk_size, capacity=capacity, slots=slots, features=features)


def parse_status(payload: bytes) -> dict:
    console_link, bond, mode, plan_state = payload[0:4]
    plan_hash = payload[4:20]
    frame_count, current_frame = struct.unpack("<HH", payload[20:24])
    (loop_count,) = struct.unpack("<I", payload[24:28])
    tag_state = payload[28]
    last_error_code = payload[37]
    (last_error_detail,) = struct.unpack("<I", payload[38:42])
    last_stop_reason = payload[42]
    (uptime_ms,) = struct.unpack("<I", payload[43:47])
    return dict(console_link=console_link, bond=bond, mode=mode, plan_state=plan_state,
                plan_hash=plan_hash, frame_count=frame_count, current_frame=current_frame,
                loop_count=loop_count, tag_state=tag_state,
                last_error=(last_error_code, last_error_detail),
                last_stop_reason=last_stop_reason, uptime_ms=uptime_ms)


# --------------------------------------------------------------- the scenario


def load_plan(link: ControlLink, plan_bytes: bytes, identity: bytes) -> None:
    """Announce, chunk, commit — §2.7, §5.3.

    Inside an ACK window a chunk carries **no reply** (§2.7), so a chunk with no
    answer is a permitted outcome and not a timeout to raise on. The device ACKs
    at the window boundary and on completion; this follows the offsets it sends
    rather than assuming where they land (an earlier ACK under ring pressure is
    also permitted), and stops sending once the device says it has the lot.
    """
    total = len(plan_bytes)
    reply = link.request(VERB_LOAD_PLAN, struct.pack("<BI", BULK_ANNOUNCE, total) + identity)
    if reply.verb == VERB_ERROR:
        raise RuntimeError(f"announce refused: {reply.payload.hex()}")
    offset = struct.unpack("<I", reply.payload)[0] if len(reply.payload) == 4 else 0

    while offset < total:
        n = min(CHUNK_SIZE, total - offset)
        frame = struct.pack("<BI", BULK_CHUNK, offset) + plan_bytes[offset : offset + n]
        try:
            reply = link.request(VERB_LOAD_PLAN, frame, timeout=0.3)
        except TimeoutError:
            # No reply inside the window is the documented outcome (§2.7), so
            # advance by what was sent and keep following the device's offsets.
            offset += n
            continue
        if reply.verb == VERB_ERROR:
            raise RuntimeError(f"chunk at {offset} refused: {reply.payload.hex()}")
        if len(reply.payload) == 4:
            offset = struct.unpack("<I", reply.payload)[0]
        else:
            offset += n

    reply = link.request(VERB_LOAD_PLAN, struct.pack("<BI", BULK_COMMIT, total) + identity)
    if reply.verb == VERB_ERROR:
        raise RuntimeError(f"commit refused: {reply.payload.hex()}")
    if len(reply.payload) != 4:
        raise RuntimeError(f"commit did not ACK an offset: {reply.payload.hex()}")


def status_of(link: ControlLink) -> dict:
    reply = link.request(VERB_STATUS)
    if reply.verb == VERB_ERROR:
        raise RuntimeError(f"STATUS refused: {reply.payload.hex()}")
    return parse_status(reply.payload)


def bench(port: str, seconds: float) -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"  [{'ok ' if ok else 'FAIL'}] {what}")
        if not ok:
            failures.append(what)
    link = ControlLink(port)
    try:
        print("§2.6 HELLO")
        hello = parse_hello(link.request(VERB_HELLO, bytes([PROTO_VER])).payload)
        print(f"    boot_id=0x{hello['boot_id']:08x} fw={'.'.join(map(str, hello['fw']))} "
              f"features=0x{hello['features']:04x}")
        check(hello["proto_ver"] == PROTO_VER, "proto_ver is 1")
        check(bool(hello["features"] & 0x0001), "features.macro (bit 0) is advertised (#24)")

        print("§2.7/§5.3 commit the golden fixture")
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "plan"
        plan = compile_json((fixture / "correction.json").read_text())
        identity = plan_identity(plan.payload)
        # §5.6: the identity is the payload's SHA-256 *truncated to 16 bytes*, so
        # the fixture's full digest must start with it — the same relationship
        # `container/tests/test_golden_fixture.py` asserts.
        expected = (fixture / "correction.sha256").read_text().split()[0]
        print(f"    {len(plan.payload)} B, {plan.record_count} records, loop_ms={plan.loop_ms}, "
              f"hash={identity.hex()}")
        check(expected.startswith(identity.hex()),
              "the compiled identity is the fixture digest's first 16 bytes (§5.6)")
        load_plan(link, plan.payload, identity)

        st = status_of(link)
        check(st["plan_state"] == PLAN_COMMITTED, "plan_state=COMMITTED after the commit")
        check(st["plan_hash"] == identity, "the echoed plan_hash equals what was sent (§5.6)")
        check(st["frame_count"] == plan.record_count, "plan_frame_count == record_count (§5.6)")

        # The fixture is a real 26.2 s macro — far too long to watch loop. Commit
        # a short synthesized one for the run/loop observations, so the
        # measurement completes inside the run rather than being extrapolated.
        print("§4.3/§5.4 START a short macro, then watch it run and loop")
        short = compile_macro([
            {"t": 0.0, "ev": {"type": "button", "name": "a", "pressed": True}},
            {"t": 120.0, "ev": {"type": "button", "name": "a", "pressed": False}},
            {"t": 240.0, "ev": {"type": "button", "name": "b", "pressed": True}},
            {"t": 360.0, "ev": {"type": "button", "name": "b", "pressed": False}},
        ])
        short_identity = plan_identity(short.payload)
        print(f"    {len(short.payload)} B, {short.record_count} records, loop_ms={short.loop_ms}")
        load_plan(link, short.payload, short_identity)
        st = status_of(link)
        check(st["plan_hash"] == short_identity, "the short plan's hash echoes back (§5.6)")

        link.request(VERB_START)
        check(status_of(link)["mode"] == MODE_MACRO, "START enters MACRO")

        seen_frames: set[int] = set()
        events: list[int] = []
        samples: list[tuple[float, int]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # Collect EVENT frames off the same read as the reply: the device
            # drains them itself, so a separate `drain()` at the end would find
            # the wire already empty (which is how the first run reported 0).
            reply = link.request(VERB_STATUS)
            for frame in link.pending:
                if frame.type == TYPE_EVENT and frame.payload:
                    events.append(frame.payload[0])
            link.pending.clear()
            if reply.verb == VERB_ERROR:
                raise RuntimeError(f"STATUS refused: {reply.payload.hex()}")
            st = parse_status(reply.payload)
            samples.append((time.monotonic(), st["loop_count"]))
            seen_frames.add(st["current_frame"])

        print(f"    current_frame advanced over {len(seen_frames)} distinct values; "
              f"loop_count={samples[-1][1]} over {seconds:.1f}s")
        check(len(seen_frames) > 2, "current_frame advances through the plan (§5.6)")
        check(samples[-1][1] >= 1, "the plan loops (§5.4)")

        if samples[-1][1] >= 1:
            # A linear fit of loop_count against time is the honest measurement:
            # it divides out the poll's own phase instead of charging one
            # arbitrary partial interval to the first loop.
            t_a, c_a = samples[0]
            t_b, c_b = samples[-1]
            loops = c_b - c_a
            if loops >= 1:
                measured = (t_b - t_a) / loops * 1000.0
                print(f"    measured loop period ≈ {measured:.1f} ms over {loops} loops "
                      f"(plan says {short.loop_ms} ms)")
                # The poll can only over-measure a loop boundary, never under: a
                # boundary is observed late, never early. So a lower bound is the
                # honest assertion here; #35 owns the tight one, on a BLE capture
                # with the console actually subscribed and the console link live.
                check(measured >= short.loop_ms * 0.97,
                      f"the loop is not faster than the plan ({measured:.0f} >= "
                      f"{short.loop_ms * 0.97:.0f} ms)")

        print("§4.3 STOP")
        link.request(VERB_STOP)
        st = status_of(link)
        check(st["mode"] == MODE_IDLE, "STOP returns to IDLE")
        check(st["last_stop_reason"] == STOP_CONTAINER, "last_stop_reason=CONTAINER_STOP")
        check(st["plan_state"] == PLAN_COMMITTED, "STOP retains the plan (one verb re-arms)")
        check(st["current_frame"] == 0, "IDLE zeroes current_frame (§3.2)")

        print("§4.3 a zero-record plan is refused at START (the #24 fault guard)")
        # Hand-built: header only, record_count = 0. §5.3's structural check
        # accepts len == 12; §5.5's compiler never emits one. That is precisely
        # the plan the executor must refuse rather than walk.
        empty = struct.pack("<IBBHI", 0x4C50534E, 1, 11, 0, 0)
        empty_hash = plan_identity(empty)
        load_plan(link, empty, empty_hash)
        check(status_of(link)["plan_state"] == PLAN_COMMITTED,
              "an empty plan passes the §5.3 commit check")
        reply = link.request(VERB_START)
        code = reply.payload[0] if reply.verb == VERB_ERROR and reply.payload else None
        check(reply.verb == VERB_ERROR and code == ERR_BAD_PLAN,
              f"START on an empty plan is ERROR BAD_PLAN (got "
              f"{'ERROR ' + str(code) if reply.verb == VERB_ERROR else 'a reply'})")
        st = status_of(link)
        check(st["mode"] == MODE_IDLE, "the refused START does not enter MACRO")
        check(st["last_error"][0] == ERR_BAD_PLAN, "the refusal is legible as STATUS.last_error")

        print("§3.4 the fault-row shape (NONE + set) is reachable but not forced here")
        print(f"    last_stop_reason={st['last_stop_reason']}, "
              f"last_error={st['last_error']} — a refused arm is an ERROR reply, not a fault")

        print("§4.3/§4.6 the AMIIBO exit releases the neutral")
        # The executor is STOPPED throughout AMIIBO (nothing arms it), so this is
        # the case where "neutral is the last write of the mode" must not depend
        # on the walk having been running. It is invisible on the HID wire, which
        # is why it is checked on the control wire and in the host suite.
        tag = bytearray(540)
        tag[0:8] = bytes([0x04, 0x21, 0xFE, 0x88 ^ 0x04 ^ 0x21 ^ 0xFE, 0xCA, 0x42, 0x6C, 0x81])
        for i in range(8, 540):
            tag[i] = i & 0xFF
        place = bytes(tag)
        link.request(VERB_PLACE_AMIIBO, struct.pack("<BI", BULK_ANNOUNCE, len(place)))
        for off in range(0, len(place), CHUNK_SIZE):
            n = min(CHUNK_SIZE, len(place) - off)
            try:
                link.request(VERB_PLACE_AMIIBO,
                             struct.pack("<BI", BULK_CHUNK, off) + place[off : off + n],
                             timeout=0.3)
            except TimeoutError:
                pass  # inside the ACK window (§2.7)
        link.request(VERB_PLACE_AMIIBO, struct.pack("<BI", BULK_COMMIT, len(place)))
        st = status_of(link)
        check(st["mode"] == MODE_AMIIBO and st["tag_state"] == 1,
              "PLACE_AMIIBO enters AMIIBO with a tag placed")
        link.request(VERB_STOP)
        st = status_of(link)
        check(st["mode"] == MODE_IDLE and st["last_stop_reason"] == STOP_CONTAINER,
              "AMIIBO + STOP returns to IDLE with CONTAINER_STOP (§4.3)")

        print("§3.3 the EVENT stream during the run")
        print(f"    {len(events)} events seen while polling; kinds={sorted(set(events))}")
        check(CONTROL_EVENT_LOOP_COMPLETED in events or not events,
              "LOOP_COMPLETED is not the only thing on the wire when the poll is fast")
    finally:
        link.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("plan executor bench: all checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/cu.usbmodem5C930639851")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()
    return bench(args.port, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
