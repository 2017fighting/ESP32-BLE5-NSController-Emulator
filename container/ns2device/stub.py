"""`StubDevice` — a `DeviceApi` with no port, for the container skeleton (#29).

It is a *faithful* stub rather than a mock: it holds the §4.3 mode axis, the
plan/tag retention rules, the §2.5 typed rejections and the §3.3 event kinds,
so the four screens are exercised against real transitions and the following
ticket (#30) has a contract to satisfy, not a wish. It deliberately does **not**
do the things the device does not: it holds no key, emits no `STATUS` cadence
(the container polls), and runs no real-time loop — the running macro's frame
and loop counters are *computed* from the injected clock, which is what makes
them testable.

Ticket #30 replaces this with the session over `FrameIO`; the interface it
implements is `ns2device.api.DeviceApi`.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
from bisect import bisect_right
from collections.abc import AsyncIterator, Callable

from ..ns2plan.format import (
    PLAN_FORMAT_VERSION,
    PLAN_HEADER_SIZE,
    PLAN_MAGIC,
    PLAN_RECORD_SIZE,
)
from ..ns2sealing import identity_of
from .model import (
    Bond,
    CommandError,
    ConsoleConfig,
    ConsoleLink,
    ConsolePolling,
    ErrorCode,
    Event,
    EventKind,
    Hello,
    LinkEdge,
    Mode,
    PlanState,
    Status,
    StopReason,
    TagState,
)

TAG_IMAGE_LENGTH = 540
DEFAULT_CONSOLE_SCAN_MS = 3000
_STREAM_END = object()


def _parse_plan(payload: bytes) -> tuple[int, int, list[int]]:
    """§5.3's structural check, returning `(record_count, loop_ms, frame_ends)`.

    Raises `ValueError` for anything the device could not walk. The stub runs
    the same check the device does at commit, so `BAD_PLAN` is exercised here
    rather than only on the bench.
    """
    if len(payload) < PLAN_HEADER_SIZE:
        raise ValueError("plan shorter than its header")
    magic, version, record_size, record_count = struct.unpack_from("<IBBH", payload, 0)
    loop_ms = struct.unpack_from("<I", payload, 8)[0]
    if magic != PLAN_MAGIC:
        raise ValueError("not an NSPL plan")
    if version != PLAN_FORMAT_VERSION:
        raise ValueError(f"unsupported plan format version {version}")
    if record_size != PLAN_RECORD_SIZE:
        raise ValueError(f"unsupported record size {record_size}")
    expected = PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * record_count
    if len(payload) != expected:
        raise ValueError(f"plan is {len(payload)} bytes, header says {expected}")
    if record_count == 0:
        # §4.3: structurally valid yet unrunnable; START is what refuses it.
        return 0, loop_ms, []
    ends: list[int] = []
    offset = PLAN_HEADER_SIZE
    total = 0
    for _ in range(record_count):
        total += struct.unpack_from("<H", payload, offset + 9)[0]
        ends.append(total)
        offset += PLAN_RECORD_SIZE
    if total != loop_ms:
        raise ValueError(f"sum(hold)={total} but loop_ms={loop_ms}")
    return record_count, loop_ms, ends


class StubDevice:
    """An in-memory device that satisfies `DeviceApi`. See the module docstring."""

    def __init__(
        self,
        *,
        boot_id: int = 0x8F21C4A0,
        features: int = 0b111,
        now: Callable[[], float] = time.monotonic,
        console_link: ConsoleLink = ConsoleLink.CONNECTED,
        bond: Bond = Bond.PAIRED,
        console_scan_ms: int = DEFAULT_CONSOLE_SCAN_MS,
    ) -> None:
        self._now = now
        self._boot_at = now()
        self.hello_state = Hello(boot_id=boot_id, features=features)
        self.config_state = ConsoleConfig()
        self._console_link = console_link
        self._bond = bond
        self._console_scan_ms = console_scan_ms

        self._mode = Mode.IDLE
        self._last_stop_reason = StopReason.NONE
        self._last_error: CommandError | None = None

        self._plan: bytes | None = None
        self._plan_hash: bytes = bytes(16)
        self._record_count = 0
        self._loop_ms = 0
        self._frame_ends: list[int] = []
        self._started_at: float | None = None

        self._tag: bytes | None = None
        self._tag_identity: bytes = bytes(7)
        self._placed_at: float | None = None

        self._subscribers: list[asyncio.Queue[object]] = []
        self._closed = False

    # ── test/debug affordances (not part of DeviceApi) ─────────────────────

    @property
    def plan(self) -> bytes | None:
        return self._plan

    @property
    def tag(self) -> bytes | None:
        return self._tag

    def set_console_link(self, link: ConsoleLink, *, reason: int = 0) -> None:
        self._console_link = link
        which = LinkEdge.CONNECTED if link is ConsoleLink.CONNECTED else LinkEdge.DISCONNECTED
        self._emit(EventKind.CONSOLE_LINK, struct.pack("<BH", int(which), reason))

    def reboot(self, *, boot_id: int | None = None) -> None:
        """Simulate a power cycle: new `boot_id`, plan and tag cleared (ADR-0004)."""
        self._boot_at = self._now()
        if boot_id is not None:
            self.hello_state = Hello(
                boot_id=boot_id,
                fw_major=self.hello_state.fw_major,
                fw_minor=self.hello_state.fw_minor,
                fw_patch=self.hello_state.fw_patch,
                fw_build=self.hello_state.fw_build,
                features=self.hello_state.features,
            )
        self._plan = None
        self._plan_hash = bytes(16)
        self._frame_ends = []
        self._record_count = 0
        self._tag = None
        self._tag_identity = bytes(7)
        self._mode = Mode.IDLE
        self._started_at = None
        self._last_stop_reason = StopReason.NONE
        self._last_error = None
        self._emit(EventKind.BOOT, struct.pack("<I", self.hello_state.boot_id))

    def boot_button(self, *, long: bool) -> None:
        """The BOOT panic stop (§4.5): stop on the press edge; a long press forgets."""
        had_state = self._mode is not Mode.IDLE
        if self._mode is Mode.MACRO:
            self._mode = Mode.IDLE
            self._started_at = None
            self._last_stop_reason = StopReason.BOOT_LOCAL
            self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.IDLE)]))
        elif self._mode is Mode.AMIIBO:
            self._unplace_emit()
            self._mode = Mode.IDLE
            self._last_stop_reason = StopReason.BOOT_LOCAL
            self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.IDLE)]))
        if long and (self._plan is not None or self._tag is not None or had_state):
            self._plan = None
            self._plan_hash = bytes(16)
            self._frame_ends = []
            self._record_count = 0
            self._tag = None
            self._tag_identity = bytes(7)
            self._emit(EventKind.PLAN_DISCARDED)

    # ── DeviceApi ──────────────────────────────────────────────────────────

    async def open(self) -> None:
        """The stub has no port; there is nothing to open."""

    async def hello(self) -> Hello:
        return self.hello_state

    async def status(self) -> Status:
        current_frame, loop_count = self._progress()
        polling = (
            ConsolePolling.POLLING
            if self._tag is not None and self._console_link is ConsoleLink.CONNECTED
            else ConsolePolling.IDLE
        )
        return Status(
            console_link=self._console_link,
            bond=self._bond,
            mode=self._mode,
            plan_state=PlanState.COMMITTED if self._plan is not None else PlanState.NONE,
            plan_hash=self._plan_hash,
            plan_frame_count=self._record_count,
            current_frame=current_frame,
            loop_count=loop_count,
            tag_state=TagState.PLACED if self._tag is not None else TagState.NONE,
            tag_identity=self._tag_identity,
            console_polling=polling,
            last_error_code=self._last_error.code if self._last_error else ErrorCode.NONE,
            last_error_detail=self._last_error.detail if self._last_error else 0,
            last_stop_reason=self._last_stop_reason,
            uptime_ms=int((self._now() - self._boot_at) * 1000),
        )

    async def load_plan(self, plan_hash: bytes, plan: bytes, *, on_progress=None) -> None:
        if self._mode is not Mode.IDLE:
            raise self._reject(ErrorCode.BAD_STATE, int(self._mode))
        if len(plan) > self.hello_state.plan_capacity_bytes:
            raise self._reject(ErrorCode.PLAN_TOO_LARGE, len(plan))
        try:
            record_count, loop_ms, ends = _parse_plan(bytes(plan))
        except ValueError:
            raise self._reject(ErrorCode.BAD_PLAN, 0) from None
        if bytes(plan_hash) != _identity(bytes(plan)):
            raise self._reject(ErrorCode.BAD_PLAN, 0)
        self._stream_progress(len(plan), on_progress)
        self._plan = bytes(plan)
        self._plan_hash = bytes(plan_hash)
        self._record_count = record_count
        self._loop_ms = loop_ms
        self._frame_ends = ends
        self._clear_error()
        self._emit(EventKind.PLAN_COMMITTED, self._plan_hash)

    async def start(self) -> None:
        if self._mode is Mode.IDLE and self._plan is None:
            raise self._reject(ErrorCode.NO_PLAN, 0)
        if self._mode is not Mode.IDLE:
            raise self._reject(ErrorCode.ALREADY_RUNNING, 0)
        if self._record_count == 0:
            # §4.3: committed yet unrunnable — refused before the mode moves.
            raise self._reject(ErrorCode.BAD_PLAN, 0)
        self._clear_error()
        self._mode = Mode.MACRO
        self._started_at = self._now()
        self._last_stop_reason = StopReason.NONE
        self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.MACRO)]))

    async def stop(self) -> None:
        was_idle = self._mode is Mode.IDLE
        if self._mode is Mode.AMIIBO:
            self._unplace_emit()
        self._mode = Mode.IDLE
        self._started_at = None
        if not was_idle:
            self._last_stop_reason = StopReason.CONTAINER_STOP
            self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.IDLE)]))
        self._clear_error()

    async def place(self, tag: bytes, *, on_progress=None) -> None:
        if self._mode is Mode.MACRO:
            raise self._reject(ErrorCode.BAD_STATE, int(self._mode))
        if len(tag) != TAG_IMAGE_LENGTH:
            raise self._reject(ErrorCode.BAD_LENGTH, len(tag))
        self._stream_progress(len(tag), on_progress)
        replaced = self._mode is Mode.AMIIBO
        if replaced:
            self._unplace_emit()
        self._clear_error()
        self._tag = bytes(tag)
        self._tag_identity = identity_of(tag)
        self._placed_at = self._now()
        self._mode = Mode.AMIIBO
        self._last_stop_reason = StopReason.NONE
        if not replaced:
            self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.AMIIBO)]))
        self._emit(EventKind.TAG_PLACED, self._tag_identity)

    async def unplace(self) -> None:
        if self._tag is not None:
            self._unplace_emit()
            self._mode = Mode.IDLE
            self._emit(EventKind.MODE_CHANGED, bytes([int(Mode.IDLE)]))
        self._clear_error()

    async def pair_unpair(self) -> None:
        self._bond = Bond.UNPAIRED
        self._clear_error()

    async def config(self, *, report_interval_ms: int, led: bool) -> None:
        if report_interval_ms <= 0:
            raise self._reject(ErrorCode.BAD_LENGTH, report_interval_ms)
        self.config_state = ConsoleConfig(report_interval_ms=report_interval_ms, led=bool(led))
        self._clear_error()

    def events(self) -> AsyncIterator[Event]:
        return self._event_stream()

    def log_lines(self) -> AsyncIterator[tuple[str, str]]:
        """The stub owns no port, so it has no device log to demux (#30 real)."""
        return self._log_stream()

    async def close(self) -> None:
        self._closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(_STREAM_END)

    # ── internals ──────────────────────────────────────────────────────────

    def _progress(self) -> tuple[int, int]:
        if self._mode is not Mode.MACRO or self._started_at is None or self._loop_ms <= 0:
            return 0, 0
        elapsed_ms = int((self._now() - self._started_at) * 1000)
        loop_count, within = divmod(elapsed_ms, self._loop_ms)
        current = bisect_right(self._frame_ends, within)
        if current >= self._record_count:
            current = self._record_count - 1
        return current, loop_count

    @property
    def scan_count(self) -> int:
        """Stub-only console scan counter: one per `console_scan_ms` placed."""
        if self._tag is None or self._placed_at is None:
            return 0
        return 1 + int((self._now() - self._placed_at) * 1000 // self._console_scan_ms)

    def _unplace_emit(self) -> None:
        self._tag = None
        self._tag_identity = bytes(7)
        self._placed_at = None
        self._emit(EventKind.TAG_UNPLACED)

    def _stream_progress(self, total: int, on_progress) -> None:
        """Replay the windowed path's offsets so a caller can render progress.

        The stub commits synchronously; a real session drives this from the
        ACK'd offsets (§2.7). The last callback is always `(total, total)`.
        """
        if on_progress is None or total <= 0:
            return
        step = self.hello_state.chunk_size
        offset = 0
        while offset < total:
            offset = min(total, offset + step)
            on_progress(offset, total)

    def _clear_error(self) -> None:
        self._last_error = None

    def _reject(self, code: ErrorCode, detail: int) -> CommandError:
        error = CommandError(code, detail)
        self._last_error = error
        self._emit(EventKind.ERROR_RAISED, error.to_bytes())
        return error

    def _emit(self, kind: EventKind, raw: bytes = b"") -> None:
        event = Event(kind, raw)
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def _event_stream(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                assert isinstance(item, Event)
                yield item
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    async def _log_stream(self) -> AsyncIterator[tuple[str, str]]:
        for line in ():
            yield line


def _identity(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()[:16]


__all__ = ["DEFAULT_CONSOLE_SCAN_MS", "StubDevice", "TAG_IMAGE_LENGTH"]
