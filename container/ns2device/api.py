"""The two interfaces the device seam exposes (§8.2).

```text
web (aiohttp)  ──DeviceApi──▶  device (session)  ──FrameIO──▶  serial (transport)
```

`DeviceApi` is what the container's state model and the web seam call. It
speaks in verbs and values, never bytes and never the port: `status()`,
`load_plan(hash, bytes)`, `start()`, `stop()`, `place(tag)`, `unplace()`,
`pair_unpair()`, `config(**kw)` (§8.2). Note the shape rather than a rule:

- **One outstanding control request** — every method awaits its reply, so there
  is nowhere to put a retry loop, and retrying `START` would be a second start.
- **Bulk is the one exception** (§2.7, ADR-0015) and it is still one call: the
  windowed stream is `load_plan`/`place`'s internal business, and `on_progress`
  is how the caller learns the ACK'd offset without owning the window.

`FrameIO` is what the device session uses to reach the board. It is bytes and
verbs, and its real implementation is issue #30's; this module only fixes the
interface the following tickets fill in.

Issue #29 ships a `StubDevice` (see `stub.py`) that implements `DeviceApi`
without a port; ticket #30 replaces it with the session over `FrameIO`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol, runtime_checkable

from .model import CommandError, Event, Hello, Status, Verb

#: `(offset, total)` — the bytes written so far and the transfer size.
ProgressCallback = Callable[[int, int], None]


@runtime_checkable
class DeviceApi(Protocol):
    """The device as the container sees it: values, verbs, no port."""

    async def open(self) -> None:
        """Open the control link. Idempotent; the first step of every attach.

        `HELLO` is the first frame after this, on every attach and reconnect
        (§2.8). A dead link is reopened here and nowhere else, so no verb can
        reach the board between a link transition and its `HELLO`.
        """
        ...

    async def hello(self) -> Hello:
        """Send `HELLO` and return the capabilities reply (§2.6)."""
        ...

    async def status(self) -> Status:
        """Read the fixed 47-byte `STATUS` (§3.2)."""
        ...

    async def load_plan(
        self,
        plan_hash: bytes,
        plan: bytes,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Upload a plan over the windowed bulk path and commit it (§2.7).

        Raises `CommandError` (e.g. `PLAN_TOO_LARGE`, `BAD_PLAN`, `BAD_STATE`)
        without touching device state.
        """
        ...

    async def start(self) -> None:
        """`START`. Raises `NO_PLAN`, `ALREADY_RUNNING` or `BAD_PLAN`."""
        ...

    async def stop(self) -> None:
        """`STOP`. Idempotent in `IDLE` (§4.4)."""
        ...

    async def place(
        self,
        tag: bytes,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Upload 540 tag bytes and place them atomically (§2.7, §4.3)."""
        ...

    async def unplace(self) -> None:
        """`UNPLACE_AMIIBO`. Idempotent when no tag is placed."""
        ...

    async def pair_unpair(self) -> None:
        """`PAIR_UNPAIR`: always *forget the bond and go pairable* (ADR-0013)."""
        ...

    async def config(self, *, report_interval_ms: int, led: bool) -> None:
        """`CONFIG`; applied at the next loop boundary in `MACRO` (§2.9)."""
        ...

    def events(self) -> AsyncIterator[Event]:
        """The `EVENT` stream. A subscription is a level, not a queue of facts:

        each event is a prompt to re-read `STATUS` (§3.1).
        """
        ...

    def log_lines(self) -> AsyncIterator[tuple[str, str]]:
        """The board's `ESP_LOG` text, demuxed from the frame stream (§8.2).

        Yields `(level, message)`, where `level` is `debug`/`info`/`warn`/`error`
        and `message` has the ESP-IDF prefix stripped. The container feeds these
        to the Logs screen as `{source="device"}` lines (§8.7). A stub device
        has nothing to say and yields nothing.
        """
        ...

    async def close(self) -> None:
        """Release the port. DTR/RTS are deasserted again in the caller's
        `finally` (§8.3, §10.3)."""
        ...


@runtime_checkable
class FrameIO(Protocol):
    """The serial transport's framing interface — issue #30's to implement."""

    async def open(self) -> None:
        """Open the port and start reading. Idempotent; reopens a dead link."""
        ...

    async def request(self, verb: Verb, payload: bytes = b"") -> bytes:
        """Send one `REQUEST` and await its `REPLY` payload.

        Raises `CommandError` on an `ERROR` reply. One outstanding request.
        """
        ...

    async def bulk(
        self,
        verb: Verb,
        payload: bytes,
        *,
        chunk_size: int = 256,
        plan_hash: bytes | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """The §2.7 windowed stream: announce, chunks, windowed ACK, commit.

        Resumes from the ACK'd offset on a retry at **window granularity**.
        `plan_hash` is the 16-byte identity a `LOAD_PLAN` carries and a
        `PLACE_AMIIBO` does not (§2.7).
        """
        ...

    def events(self) -> AsyncIterator[Event]:
        """The device's `EVENT` frames, resynchronised past `ESP_LOG` noise."""
        ...

    def log_lines(self) -> AsyncIterator[tuple[str, str]]:
        """`ESP_LOG` text demuxed out of the byte stream (§8.2's "log demux").

        `ESP_LOG` shares the wire and never contains `0x00`, so a log line
        arrives as a COBS block that fails the CRC. Demuxing surfaces it as
        `{source="device"}` lines for the Logs screen instead of silently
        counting it as a dropped frame.
        """
        ...

    async def close(self) -> None:
        """Release the port and stop the reader."""
        ...


__all__ = ["CommandError", "DeviceApi", "FrameIO", "ProgressCallback"]
