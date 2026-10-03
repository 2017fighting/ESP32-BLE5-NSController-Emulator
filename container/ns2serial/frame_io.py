"""`FrameIO` over a byte stream: the §2.3 conversation and the §2.7 bulk stream.

This is the middle seam of §8.2 — above the port (`ns2serial.port`) and below
the verbs (`ns2device.session`). It owns exactly four things:

- **Framing.** `encode_request` out, `FrameDecoder` in, with the §2.2
  resynchronisation model. A bad block costs one block, never the link.
- **One outstanding control request.** `request()` and `bulk()` share one
  `asyncio.Lock`, so there is nowhere to put a retry of a mode verb — a retried
  `START` is a second start (§2.3, ADR-0007).
- **The bulk window** (§2.7, ADR-0015). A plan or a tag is announced, sent as
  chunks, paced by offset-keyed ACKs at a 4096-byte window, and committed. A
  chunk inside a window carries no reply, so this is the one place replies are
  consumed as a stream rather than one per request.
- **The event and log streams.** `EVENT` frames carry no reply and mark an edge
  (§3.3); a discarded segment that is printable text is an `ESP_LOG` line, and
  is surfaced as `(level, message)` rather than silently counted as noise
  (§8.2's "log demux", §8.7's `{source, level, message}`).

**Reconnection is explicit.** The port is opened by `open()`, and `request()`
refuses to send anything while the link is not open. When the link dies the read
loop marks the transport dead and wakes every waiter; the device session's
`open()` tears the dead port down and opens a fresh one, and `HELLO` is the next
frame after it (§2.8). No verb is ever rewritten, and no verb reaches the device
between a link transition and its `HELLO`.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator, Callable

from ..ns2device.model import (
    CHUNK_SIZE,
    MAX_FRAME,
    PROTO_VERSION,
    CommandError,
    Event,
    EventKind,
    FrameType,
    ProtocolError,
    Verb,
)
from .api import SerialTransport, TransportUnavailable
from .framing import Frame, FrameDecoder, encode_request

#: §2.7: the ACK window. The device may ACK earlier; this is the container's cap
#: on un-ACKed bytes in flight.
ACK_WINDOW = 4096

#: §2.7: every bulk frame's payload begins with a one-byte `op`.
BULK_ANNOUNCE = 1
BULK_CHUNK = 2
BULK_COMMIT = 3

_LOG_LEVELS = {"V": "debug", "D": "debug", "I": "info", "W": "warn", "E": "error"}

#: Marks the reply queue when the read loop dies, so a waiter wakes instead of hanging.
_CLOSED = object()

#: Marks the reply queue when a reply's header `ver` is not this protocol's (§2.8).
_BAD_VERSION = object()


class FrameTransport:
    """The `FrameIO` protocol's implementation (§8.2)."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        request_timeout: float = 2.0,
        bulk_timeout: float = 5.0,
        bulk_retries: int = 3,
        ack_window: int = ACK_WINDOW,
        max_frame: int = MAX_FRAME,
        read_size: int = 4096,
    ) -> None:
        self._transport = transport
        self._request_timeout = request_timeout
        self._bulk_timeout = bulk_timeout
        self._bulk_retries = bulk_retries
        self.ack_window = ack_window
        self.max_frame = max_frame
        self._read_size = read_size

        self._decoder = FrameDecoder(max_frame=max_frame, on_discard=self._on_noise)
        self._replies: asyncio.Queue = asyncio.Queue()
        self._events: asyncio.Queue = asyncio.Queue()
        self._logs: asyncio.Queue = asyncio.Queue()
        self._control = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._state = "closed"  # closed | open | dead
        self._failure: BaseException | None = None
        self._closing = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def port(self) -> str:
        return self._transport.port

    @property
    def baud(self) -> int:
        return self._transport.baud

    async def open(self) -> None:
        """Open the port and start reading. Idempotent; reopens a dead link."""
        async with self._lifecycle:
            if self._state == "dead":
                await self._teardown()
            await self._open_locked()

    async def close(self) -> None:
        """Release the port for good. Idempotent."""
        self._closing = True
        async with self._lifecycle:
            await self._teardown()
        self._replies.put_nowait(_CLOSED)
        self._events.put_nowait(_CLOSED)
        self._logs.put_nowait(_CLOSED)

    def _require_open(self) -> None:
        """§2.8: no verb reaches the device without a `HELLO` on this connection.

        A dead link is reopened by the device session's `open()`, which is where
        the `HELLO` and the `boot_id` branch live — never by a stray verb.
        """
        if self._state != "open":
            if self._closing:
                raise TransportUnavailable("the control link is closed")
            raise TransportUnavailable(f"{self.port} is not open")

    async def _open_locked(self) -> None:
        if self._state == "open":
            return
        if self._closing:
            raise TransportUnavailable("the control link is closed")
        self._failure = None
        self._decoder = FrameDecoder(max_frame=self.max_frame, on_discard=self._on_noise)
        self._drain_replies()
        await self._transport.open()
        self._state = "open"
        self._reader = asyncio.create_task(self._read_loop(), name="ns2serial-read")

    async def _teardown(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._state = "closed"
        with contextlib.suppress(Exception):
            await self._transport.close()

    async def _read_loop(self) -> None:
        try:
            while True:
                data = await self._transport.read(self._read_size)
                if not data:
                    raise TransportUnavailable(f"{self.port} reached end of file")
                for frame in self._decoder.feed(data):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surfaced through the waiters
            failure = exc if isinstance(exc, TransportUnavailable) else TransportUnavailable(str(exc))
            self._fail(failure)

    def _fail(self, failure: TransportUnavailable) -> None:
        if self._state != "open":
            return
        self._state = "dead"
        self._failure = failure
        self._replies.put_nowait(_CLOSED)
        self._events.put_nowait(_CLOSED)

    def _dispatch(self, frame: Frame) -> None:
        # §2.8: the header's `ver` is checked on every frame before the payload is
        # interpreted, on both sides of the wire. A frame from another version is
        # not a reply this session can read; a pending request is told so rather
        # than left to time out.
        if frame.ver != PROTO_VERSION:
            if frame.type == int(FrameType.REPLY):
                self._replies.put_nowait(_BAD_VERSION)
            return
        if frame.type == int(FrameType.EVENT):
            if frame.verb != 0 or not frame.payload:
                return
            try:
                kind = EventKind(frame.payload[0])
            except ValueError:
                return  # a kind outside §3.3's closed set: ignore, never guess
            self._events.put_nowait(Event(kind, frame.payload[1:]))
        elif frame.type == int(FrameType.REPLY):
            self._replies.put_nowait(frame)
        # A REQUEST arriving here is the device's own direction; §2.8 discards it.

    async def _write(self, verb: int, payload: bytes) -> None:
        await self._transport.write(encode_request(int(verb), payload, ver=PROTO_VERSION))

    # ── FrameIO ────────────────────────────────────────────────────────────

    async def request(self, verb: Verb, payload: bytes = b"") -> bytes:
        """Send one `REQUEST` and return its `REPLY` payload, or raise `CommandError`."""
        async with self._control:
            self._require_open()
            self._drain_replies()
            frame = await self._exchange(verb, payload, self._request_timeout)
            return frame.payload

    async def bulk(
        self,
        verb: Verb,
        payload: bytes,
        *,
        chunk_size: int = CHUNK_SIZE,
        plan_hash: bytes | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Announce, stream and commit one `LOAD_PLAN`/`PLACE_AMIIBO` transfer.

        Resumes from the ACK'd offset on a timeout, at window granularity
        (§2.7 rule 5, ADR-0015). An `ERROR` reply aborts and the device discards
        its staging buffer.
        """
        data = bytes(payload)
        total = len(data)
        plan_hash_bytes = b"" if plan_hash is None else bytes(plan_hash)
        if len(plan_hash_bytes) not in (0, 16):
            raise ValueError("plan_hash must be 16 bytes or None")

        async with self._control:
            self._require_open()
            self._drain_replies()

            frame = await self._exchange(
                verb, struct.pack("<BI", BULK_ANNOUNCE, total) + plan_hash_bytes, self._request_timeout
            )
            acked = self._ack_offset(frame, total)
            if on_progress is not None:
                on_progress(acked, total)

            sent = 0
            retries = 0
            while sent < total or acked < total:
                while sent < total and (sent - acked) < self.ack_window:
                    end = min(total, sent + chunk_size)
                    await self._write(verb, struct.pack("<BI", BULK_CHUNK, sent) + data[sent:end])
                    sent = end
                try:
                    frame = await self._next_reply(verb, self._bulk_timeout)
                except TransportUnavailable:
                    retries += 1
                    if retries > self._bulk_retries:
                        raise
                    sent = acked  # window granularity: resend from next_expected_offset
                    continue
                retries = 0
                offset = self._ack_offset(frame, total)
                if offset < acked or offset > sent:
                    raise TransportUnavailable(
                        f"the device ACK'd offset {offset} outside [acked={acked}, sent={sent}]"
                    )
                acked = offset
                if on_progress is not None:
                    on_progress(acked, total)

            frame = await self._exchange(
                verb, struct.pack("<BI", BULK_COMMIT, total) + plan_hash_bytes, self._request_timeout
            )
            final = self._ack_offset(frame, total)
            if final != total:
                raise TransportUnavailable(
                    f"the commit ACK'd {final}, expected the whole {total}-byte transfer"
                )
            if on_progress is not None:
                on_progress(total, total)

    async def events(self) -> AsyncIterator[Event]:
        """The `EVENT` stream. A dead link is not the end of it; `close()` is."""
        async for item in self._stream(self._events):
            yield item

    async def log_lines(self) -> AsyncIterator[tuple[str, str]]:
        """`ESP_LOG` text demuxed out of the wire, as `(level, message)`."""
        async for item in self._stream(self._logs):
            yield item

    # ── internals ──────────────────────────────────────────────────────────

    async def _stream(self, queue: asyncio.Queue) -> AsyncIterator:
        """Drain a queue until `close()`; a `_CLOSED` mark is skipped, not yielded."""
        while not self._closing:
            item = await queue.get()
            if item is _CLOSED:
                continue
            yield item

    def _drain_replies(self) -> None:
        """Drop stale replies. Safe because the caller holds the control lock."""
        while True:
            try:
                self._replies.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _exchange(self, verb: Verb, payload: bytes, timeout: float) -> Frame:
        """One request → its reply, for the verbs with exactly one reply."""
        await self._write(verb, payload)
        return await self._next_reply(verb, timeout)

    async def _next_reply(self, verb: Verb, timeout: float) -> Frame:
        while True:
            try:
                frame = await asyncio.wait_for(self._replies.get(), timeout)
            except TimeoutError:
                raise TransportUnavailable(
                    f"no reply to {Verb(int(verb)).name} within {timeout:.1f}s"
                ) from None
            if frame is _CLOSED:
                raise self._failure if self._failure is not None else TransportUnavailable(
                    "the control link died while a request was outstanding"
                )
            if frame is _BAD_VERSION:
                raise ProtocolError(
                    f"a reply to {Verb(int(verb)).name} carried a protocol version this session "
                    f"does not speak (expected {PROTO_VERSION})"
                )
            if frame.verb == int(Verb.ERROR):
                raise CommandError.from_bytes(frame.payload)
            if frame.verb == int(verb):
                return frame
            # One outstanding request means this cannot be another verb's reply
            # while we hold the control lock; it is stale, so it is ignored.

    @staticmethod
    def _ack_offset(frame: Frame, total: int) -> int:
        if len(frame.payload) != 4:
            raise ProtocolError(f"a bulk ACK was {len(frame.payload)} bytes, expected 4")
        offset = struct.unpack("<I", frame.payload)[0]
        if offset > total:
            raise ProtocolError(f"the bulk ACK named offset {offset} beyond {total} bytes")
        return offset

    def _on_noise(self, segment: bytes) -> None:
        text = _printable(segment)
        if text is None:
            return
        for line in text.splitlines():
            line = line.rstrip("\r")
            if line.strip():
                self._logs.put_nowait(_parse_log_line(line))


def _printable(segment: bytes) -> str | None:
    """The segment as text when it is plausibly `ESP_LOG`, else `None`.

    A corrupted frame also lands here, and it is usually binary; requiring every
    character to be printable keeps the Logs screen free of decoded noise while
    still catching the one case that matters — `ESP_LOG` never contains `0x00`,
    so a log line always arrives as a whole non-frame segment.
    """
    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    if all(character.isprintable() or character in "\r\n\t" for character in text):
        return text
    return None


def _parse_log_line(line: str) -> tuple[str, str]:
    """`I (1234) tag: message` → `("info", "tag: message")`; anything else is `info`."""
    if len(line) > 2 and line[1] == " " and line[0] in _LOG_LEVELS:
        rest = line[2:].lstrip()
        if rest.startswith("("):
            close = rest.find(")")
            if close != -1:
                rest = rest[close + 1 :].lstrip()
        return _LOG_LEVELS[line[0]], rest or line
    return "info", line


__all__ = [
    "ACK_WINDOW",
    "BULK_ANNOUNCE",
    "BULK_CHUNK",
    "BULK_COMMIT",
    "FrameTransport",
]
