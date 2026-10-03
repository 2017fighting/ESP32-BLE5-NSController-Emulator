"""A byte-level fake board for the serial/device seam tests (#30).

The seams under test are conversations, not codecs-in-isolation: the point is
that two *implementations* agree on frames, one-outstanding, the bulk window,
events and log noise. So this is a minimal device that speaks §2.2 framing and
the §2.7 bulk path over an in-memory duplex byte pipe — the container side uses
`ns2serial.FrameTransport` unchanged, and every byte crosses a real COBS/CRC
encode and decode.

It is deliberately not the firmware: it holds only the state the tests exercise
(HELLO/STATUS, the mode axis, the staging axis, the ACK window), and it can be
scripted to fail, to echo a mismatched hash, to emit an event, or to write log
text, which is what makes the failure paths testable without a board.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

from container.ns2device.model import (
    CHUNK_SIZE,
    FEATURE_MACRO,
    PLAN_CAPACITY_BYTES,
    PROTO_VERSION,
    Bond,
    ConsoleLink,
    ConsolePolling,
    ErrorCode,
    EventKind,
    FrameType,
    Hello,
    Mode,
    PlanState,
    STATUS_LENGTH,
    Status,
    StopReason,
    TagState,
    Verb,
)
from container.ns2serial.api import TransportUnavailable
from container.ns2serial.framing import Frame, FrameDecoder

ACK_WINDOW = 4096
TAG_SIZE = 540


class MemoryTransport:
    """One end of an in-memory duplex byte pipe (structurally a `SerialTransport`)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.peer: MemoryTransport | None = None
        self.written = bytearray()
        self._buffer = bytearray()
        self._waiter: asyncio.Future | None = None
        self._closed = False
        self.opened = False

    def connect(self, peer: MemoryTransport) -> None:
        self.peer = peer
        peer.peer = self

    @property
    def port(self) -> str:
        return self.name

    @property
    def baud(self) -> int:
        return 921600

    async def open(self) -> None:
        self._closed = False
        self.opened = True

    async def close(self) -> None:
        self._closed = True
        self._wake()

    async def read(self, max_bytes: int) -> bytes:
        while True:
            if self._buffer:
                data = bytes(self._buffer[:max_bytes])
                del self._buffer[: len(data)]
                return data
            if self._closed or (self.peer is not None and self.peer._closed):
                return b""
            self._waiter = asyncio.get_running_loop().create_future()
            await self._waiter

    async def write(self, data: bytes) -> None:
        self.write_soon(data)

    def write_soon(self, data: bytes) -> None:
        """Enqueue synchronously; the fake board's reader is the only consumer."""
        if self._closed or self.peer is None or self.peer._closed:
            raise TransportUnavailable(f"{self.name} is closed")
        self.written += data
        self.peer._buffer += data
        self.peer._wake()

    def _wake(self) -> None:
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


def pipe() -> tuple[MemoryTransport, MemoryTransport]:
    host = MemoryTransport("host")
    device = MemoryTransport("device")
    host.connect(device)
    return host, device


class FakeBoard:
    """A minimal CONTROL device over a `MemoryTransport` (§2, §3, §4, §7)."""

    def __init__(
        self,
        *,
        boot_id: int = 0x11223344,
        features: int = FEATURE_MACRO,
        chunk_size: int = CHUNK_SIZE,
        plan_capacity_bytes: int = PLAN_CAPACITY_BYTES,
        console_link: ConsoleLink = ConsoleLink.CONNECTED,
        bond: Bond = Bond.PAIRED,
    ) -> None:
        self.hello = Hello(
            boot_id=boot_id,
            features=features,
            chunk_size=chunk_size,
            plan_capacity_bytes=plan_capacity_bytes,
        )
        self.status = Status(console_link=console_link, bond=bond)
        self.transport = MemoryTransport("device")

        self._decoder = FrameDecoder()
        self._task: asyncio.Task | None = None
        self.received: list[tuple[int, bytes]] = []
        self.in_flight = 0
        self.max_in_flight = 0

        self.plan: bytes | None = None
        self.plan_hash = bytes(16)
        self.tag: bytes | None = None
        self.mode = Mode.IDLE
        self.stop_reason = StopReason.NONE

        # scripting hooks
        self.hash_override: bytes | None = None
        self.error_on: dict[int, tuple[ErrorCode, int]] = {}
        self.delay_reply: float = 0.0
        self.drop_acks: int = 0  # swallow this many chunk ACKs, to force a resume

        # staging
        self._stage_verb: int | None = None
        self._stage_total = 0
        self._stage_next = 0
        self._stage_acked = 0
        self._stage_hash = bytes(16)
        self._stage = bytearray()
        self.max_unacked = 0

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="fake-board")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            data = await self.transport.read(4096)
            if not data:
                return
            for frame in self._decoder.feed(data):
                await self._handle(frame)

    # ── scripting ──────────────────────────────────────────────────────────

    def emit(self, kind: EventKind, payload: bytes = b"") -> None:
        self._send(Frame(PROTO_VERSION, int(FrameType.EVENT), 0, bytes([int(kind)]) + payload))

    def send_log(self, text: str) -> None:
        """Write `ESP_LOG` text into the stream, terminated by a delimiter.

        The real board writes a log line and the *next frame's* leading `0x00`
        ends the segment; the trailing delimiter here stands in for that, so the
        demux is deterministic in a test.
        """
        self.transport.write_soon(text.encode() + b"\n" + b"\x00")

    def reboot(self, *, boot_id: int) -> None:
        """Simulate a power cycle: fresh `boot_id`, the §4.8 boot defaults."""
        self.hello = Hello(
            boot_id=boot_id,
            features=self.hello.features,
            chunk_size=self.hello.chunk_size,
            plan_capacity_bytes=self.hello.plan_capacity_bytes,
        )
        self.status = Status(console_link=self.status.console_link, bond=self.status.bond)
        self.plan = None
        self.plan_hash = bytes(16)
        self.tag = None
        self.mode = Mode.IDLE
        self.stop_reason = StopReason.NONE
        self.emit(EventKind.BOOT, struct.pack("<I", boot_id))

    # ── request dispatch ───────────────────────────────────────────────────

    async def _handle(self, frame: Frame) -> None:
        if frame.type != int(FrameType.REQUEST):
            return
        self.received.append((frame.verb, frame.payload))
        if frame.verb in self.error_on:
            code, detail = self.error_on[frame.verb]
            self._reject(code, detail)
            return
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_reply:
                await asyncio.sleep(self.delay_reply)
            handler = {
                Verb.HELLO: self._hello,
                Verb.STATUS: self._status,
                Verb.LOAD_PLAN: self._bulk,
                Verb.PLACE_AMIIBO: self._bulk,
                Verb.START: self._start,
                Verb.STOP: self._stop,
                Verb.UNPLACE_AMIIBO: self._unplace,
                Verb.PAIR_UNPAIR: self._pair,
                Verb.CONFIG: self._config,
            }.get(Verb(frame.verb) if 1 <= frame.verb <= 10 else None)
            if handler is None:
                self._reject(ErrorCode.UNKNOWN_SUBCMD, frame.verb)
                return
            handler(frame)
        finally:
            self.in_flight -= 1

    def _hello(self, frame: Frame) -> None:
        self._reply(Verb.HELLO, self.hello.to_bytes())

    def _status(self, frame: Frame) -> None:
        status = Status(
            console_link=self.status.console_link,
            bond=self.status.bond,
            mode=self.mode,
            plan_state=PlanState.COMMITTED if self.plan is not None else PlanState.NONE,
            plan_hash=self.plan_hash if self.plan is not None else bytes(16),
            tag_state=TagState.PLACED if self.tag is not None else TagState.NONE,
            console_polling=ConsolePolling.POLLING if self.tag is not None else ConsolePolling.IDLE,
            last_stop_reason=self.stop_reason,
        )
        payload = status.to_bytes()
        assert len(payload) == STATUS_LENGTH
        self._reply(Verb.STATUS, payload)

    def _start(self, frame: Frame) -> None:
        if self.plan is None:
            self._reject(ErrorCode.NO_PLAN, 0)
            return
        if self.mode is not Mode.IDLE:
            self._reject(ErrorCode.ALREADY_RUNNING, 0)
            return
        self.mode = Mode.MACRO
        self.stop_reason = StopReason.NONE
        self.emit(EventKind.MODE_CHANGED, bytes([int(Mode.MACRO)]))
        self._reply(Verb.START, b"")

    def _stop(self, frame: Frame) -> None:
        if self.mode is not Mode.IDLE:
            self.stop_reason = StopReason.CONTAINER_STOP
            self.mode = Mode.IDLE
            self.emit(EventKind.MODE_CHANGED, bytes([int(Mode.IDLE)]))
        self._reply(Verb.STOP, b"")

    def _unplace(self, frame: Frame) -> None:
        if self.tag is not None:
            self.tag = None
            self.mode = Mode.IDLE
            self.emit(EventKind.TAG_UNPLACED)
        self._reply(Verb.UNPLACE_AMIIBO, b"")

    def _pair(self, frame: Frame) -> None:
        self.status = Status(console_link=self.status.console_link, bond=Bond.UNPAIRED)
        self._reply(Verb.PAIR_UNPAIR, b"")

    def _config(self, frame: Frame) -> None:
        self._reply(Verb.CONFIG, b"")

    # ── bulk (§2.7) ────────────────────────────────────────────────────────

    def _bulk(self, frame: Frame) -> None:
        payload = frame.payload
        if not payload:
            self._reject(ErrorCode.BAD_LENGTH, 0)
            return
        op = payload[0]
        is_plan = frame.verb == int(Verb.LOAD_PLAN)
        if op == 1:
            self._stage_verb = frame.verb
            self._stage_total = struct.unpack_from("<I", payload, 1)[0]
            self._stage_hash = payload[5:21] if is_plan else bytes(16)
            self._stage_next = 0
            self._stage_acked = 0
            self._stage = bytearray()
            self._ack(frame.verb, 0)
        elif op == 2:
            offset = struct.unpack_from("<I", payload, 1)[0]
            n = len(payload) - 5
            if self._stage_verb != frame.verb or offset != self._stage_next:
                self._ack(frame.verb, self._stage_next)
                return
            self._stage[offset : offset + n] = payload[5:]
            self._stage_next += n
            unacked = self._stage_next - self._stage_acked
            self.max_unacked = max(self.max_unacked, unacked)
            if self._stage_next == self._stage_total or unacked >= ACK_WINDOW:
                self._stage_acked = self._stage_next
                if self.drop_acks > 0:
                    self.drop_acks -= 1
                else:
                    self._ack(frame.verb, self._stage_next)
        elif op == 3:
            total = struct.unpack_from("<I", payload, 1)[0]
            done = self._stage_next
            if self._stage_verb != frame.verb or done != self._stage_total or total != done:
                self._reject(ErrorCode.BAD_PLAN if is_plan else ErrorCode.BAD_LENGTH, done)
                return
            if is_plan:
                self.plan = bytes(self._stage)
                self.plan_hash = self.hash_override or self._stage_hash
                self.emit(EventKind.PLAN_COMMITTED, self.plan_hash)
            else:
                self.tag = bytes(self._stage)
                self.mode = Mode.AMIIBO
                self.emit(EventKind.TAG_PLACED, bytes(7))
            self._stage_verb = None
            self._ack(frame.verb, done)
        else:
            self._reject(ErrorCode.BAD_LENGTH, len(payload))

    # ── encoding ───────────────────────────────────────────────────────────

    def _reply(self, verb: Verb, payload: bytes) -> None:
        self._send(Frame(PROTO_VERSION, int(FrameType.REPLY), int(verb), payload))

    def _reject(self, code: ErrorCode, detail: int) -> None:
        self._send(
            Frame(
                PROTO_VERSION,
                int(FrameType.REPLY),
                int(Verb.ERROR),
                struct.pack("<BI", int(code), detail),
            )
        )

    def _ack(self, verb: int, offset: int) -> None:
        self._send(Frame(PROTO_VERSION, int(FrameType.REPLY), verb, struct.pack("<I", offset)))

    def _send(self, frame: Frame) -> None:
        self.transport.write_soon(frame.encode())


__all__ = ["ACK_WINDOW", "FakeBoard", "MemoryTransport", "TAG_SIZE", "pipe"]
