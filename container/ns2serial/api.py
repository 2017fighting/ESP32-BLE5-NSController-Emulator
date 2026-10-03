"""The serial transport interface (§8.2, §8.3).

The framing codec is `framing.py`; this module is the port itself — the asyncio
`pyserial-asyncio` read/write pair. Ticket #30 implements it against the real
verbs; #29 fixes the shape and the one rule that has cost a bench session.

**The open sequence is fixed and non-negotiable** (§8.3, §10.3, `s3-bringup.md`
§10). The CH9102 wires DTR→GPIO0 and RTS→EN; pyserial asserts both by default,
which holds the board in reset and presents as "the device is doing nothing".
Stated as code because that is how it must be read:

```python
port = serial.Serial(PATH, BAUD, timeout=0.2, dsrdtr=False, rtscts=False)
port.setDTR(False)
port.setRTS(False)
try:
    ...
finally:
    port.setDTR(False)
    port.setRTS(False)
    port.close()
```
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: The documented default (§10.5); configuration, so a replugged board can be
#: pinned by `/dev/serial/by-id`. On macOS the node is forwarded onto this path
#: by the compose override (ADR-0014).
DEFAULT_PORT = "/dev/ttyACM0"

#: The measured default (G-1's bench, #33 — `docs/research/baud-bench.md`):
#: 115200 survives every bulk transfer to the capacity maximum on both host
#: stacks; 921600 degenerated §2.7's window blast (13–15 retries per 4 KB,
#: zero corruption) and is rejected for the bulk path.
DEFAULT_BAUD = 115200
#: The rate a custom image may still run (the wire itself is clean at
#: 921600; only the device-side bulk drain is not). Configuration, not a
#: recommendation.
FALLBACK_BAUD = 921600


class TransportUnavailable(RuntimeError):
    """The port is absent, held by another process, or not answering.

    The container surfaces this as `No device` or `Port busy — <process>`
    (§8.9); it never retries through a held port.
    """


@runtime_checkable
class SerialTransport(Protocol):
    """A byte stream on the control link, with the §8.3 line discipline applied."""

    @property
    def port(self) -> str: ...

    @property
    def baud(self) -> int: ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def read(self, max_bytes: int) -> bytes: ...

    async def write(self, data: bytes) -> None: ...


__all__ = [
    "DEFAULT_BAUD",
    "DEFAULT_PORT",
    "FALLBACK_BAUD",
    "SerialTransport",
    "TransportUnavailable",
]
