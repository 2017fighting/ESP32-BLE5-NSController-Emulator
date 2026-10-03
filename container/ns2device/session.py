"""`SessionDevice` — the `DeviceApi` over the real verbs (#30, §8.2).

`FrameIO` speaks frames; this module speaks verbs. It is the seam the container's
state model already calls: `hello()`, `status()`, `load_plan()`, `start()`,
`stop()`, `place()`, `unplace()`, `pair_unpair()`, `config()`, `events()`. The
stub that #29 shipped is the same interface; this is the real one.

Two pieces of the contract live here rather than in the framing layer:

- **The plan-hash comparison (§5.6).** The device echoes the 16-byte identity it
  was given in `STATUS.plan_hash`; it never computes one (ADR-0010). The
  container compares the echo against the hash it sent and raises
  `PlanHashMismatch` rather than letting a run be rendered as current.
- **The fixed-width payload checks.** `HELLO` is exactly 20 bytes and `STATUS`
  exactly 47 (§2.6, §3.2). Any other length is a protocol error, never padded.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from .api import FrameIO, ProgressCallback
from .model import (
    CHUNK_SIZE,
    PLAN_CAPACITY_BYTES,
    PROTO_VERSION,
    CommandError,
    ErrorCode,
    Event,
    Hello,
    PlanState,
    ProtocolError,
    Status,
    Verb,
)

_CONFIG = struct.Struct("<HB")


class PlanHashMismatch(RuntimeError):
    """The device echoed a plan identity other than the one it was sent (§5.6).

    Not a wire error: no `ERROR` code names it, because the device answered
    perfectly. It means container and device disagree about *which* bytes are
    committed, so the plan is not rendered as current and the run is not started.
    """

    def __init__(self, expected: bytes, echoed: bytes) -> None:
        self.expected = bytes(expected)
        self.echoed = bytes(echoed)
        super().__init__(
            f"the device echoed plan {self.echoed.hex()} but was sent {self.expected.hex()}"
        )


class SessionDevice:
    """The device over a `FrameIO`. See the module docstring."""

    def __init__(self, frame_io: FrameIO) -> None:
        self._io = frame_io
        self._hello: Hello | None = None

    @property
    def frame_io(self) -> FrameIO:
        return self._io

    @property
    def hello_state(self) -> Hello | None:
        """The last capabilities reply, or `None` before the first `HELLO`."""
        return self._hello

    # ── DeviceApi ──────────────────────────────────────────────────────────

    async def open(self) -> None:
        await self._io.open()

    async def hello(self) -> Hello:
        payload = await self._io.request(Verb.HELLO, bytes([PROTO_VERSION]))
        if len(payload) != 20:
            raise ProtocolError(f"HELLO was {len(payload)} bytes, expected 20")
        hello = Hello.from_bytes(payload)
        if hello.proto_ver != PROTO_VERSION:
            # §2.8: both the header `ver` and the payload's `proto_ver` must agree.
            raise ProtocolError(
                f"the device speaks protocol version {hello.proto_ver}, not {PROTO_VERSION}"
            )
        self._hello = hello
        return hello

    async def status(self) -> Status:
        payload = await self._io.request(Verb.STATUS)
        if len(payload) != 47:
            # §3.2: anything other than 47 bytes is a protocol error, never padded.
            raise ProtocolError(f"STATUS was {len(payload)} bytes, expected 47")
        return Status.from_bytes(payload)

    async def load_plan(
        self,
        plan_hash: bytes,
        plan: bytes,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        capacity = self._hello.plan_capacity_bytes if self._hello else PLAN_CAPACITY_BYTES
        if len(plan) > capacity:
            # §8.4: this is the backstop; the Controller pre-checks against
            # HELLO.plan_capacity_bytes before anything reaches the wire.
            raise CommandError(ErrorCode.PLAN_TOO_LARGE, len(plan))
        await self._io.bulk(
            Verb.LOAD_PLAN,
            bytes(plan),
            chunk_size=self._hello.chunk_size if self._hello else CHUNK_SIZE,
            plan_hash=bytes(plan_hash),
            on_progress=on_progress,
        )
        # §5.6: the container believes neither side silently — it compares the
        # echo against what it sent rather than trusting the ACK alone.
        status = await self.status()
        echoed = bytes(status.plan_hash)
        if status.plan_state is not PlanState.COMMITTED or echoed != bytes(plan_hash):
            raise PlanHashMismatch(bytes(plan_hash), echoed)

    async def start(self) -> None:
        await self._io.request(Verb.START)

    async def stop(self) -> None:
        await self._io.request(Verb.STOP)

    async def place(
        self,
        tag: bytes,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        await self._io.bulk(
            Verb.PLACE_AMIIBO,
            bytes(tag),
            chunk_size=self._hello.chunk_size if self._hello else CHUNK_SIZE,
            on_progress=on_progress,
        )

    async def unplace(self) -> None:
        await self._io.request(Verb.UNPLACE_AMIIBO)

    async def pair_unpair(self) -> None:
        await self._io.request(Verb.PAIR_UNPAIR)

    async def config(self, *, report_interval_ms: int, led: bool) -> None:
        await self._io.request(
            Verb.CONFIG, _CONFIG.pack(int(report_interval_ms), 1 if led else 0)
        )

    def events(self) -> AsyncIterator[Event]:
        return self._io.events()

    def log_lines(self) -> AsyncIterator[tuple[str, str]]:
        return self._io.log_lines()

    async def close(self) -> None:
        await self._io.close()


__all__ = ["PlanHashMismatch", "SessionDevice"]
