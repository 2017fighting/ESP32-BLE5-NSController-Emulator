"""The real serial port: the §8.3 open sequence, and `Port busy` (§8.9, #30).

This is the only module that opens the CH9102 node. `framing.py` is the pure
codec above it, `frame_io.py` is the request/ACK conversation above that, and
`ns2device.session` is the verbs above that (§8.2).

**The open sequence is fixed and non-negotiable.** The CH9102 wires DTR→GPIO0
and RTS→EN; pyserial asserts both by default, which holds the device in reset and
presents as "the device is doing nothing". The sequence in `open()` is the
§8.3 code, in that order, and `close()` repeats the deassertion in a `finally`
of its own before releasing the node:

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

**`Port busy` is a distinct failure, not a retry.** A node held by another
process is a deployment error (§10.5): the container surfaces `Port busy —
<process>` and does not retry through it. The holder name is best-effort — on
Linux and macOS it comes from `lsof -t`, and when that is unavailable the code
still classifies the failure correctly and names "another process".
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from collections import deque
from collections.abc import Callable

import serial
from serial_asyncio import SerialTransport as _AsyncioSerialTransport

from .api import DEFAULT_BAUD, DEFAULT_PORT, SerialTransport, TransportUnavailable

#: Exceptions whose text means "the node is held elsewhere" rather than "absent".
_BUSY_MARKERS = (
    "busy",
    "resource temporarily unavailable",
    "in use",
    "exclusively lock",
    "exclusive lock",
    "cannot open",
    "permission denied",
)


class PortBusy(TransportUnavailable):
    """The node exists but another process holds it (§8.9, §10.5).

    Subclasses `TransportUnavailable` so every existing "no device" path still
    catches it, while the container can branch on the type to stop retrying.
    """

    def __init__(self, port: str, holder: str | None, detail: str) -> None:
        self.port = port
        self.holder = holder
        message = f"Port busy on {port}"
        if holder:
            message = f"{message} — {holder}"
        super().__init__(f"{message} ({detail})" if detail else message)


def find_holder(port: str) -> str | None:
    """Best-effort name of the process holding `port`, or `None`.

    Advisory only: the classification of the failure never depends on it, and a
    missing `lsof` (or a race where the holder exits) is not an error.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        result = subprocess.run(
            [lsof, "-t", port],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not pids:
        return None
    return f"pid {', '.join(pids)}"


def _classify(port: str, exc: BaseException) -> TransportUnavailable:
    detail = str(exc) or exc.__class__.__name__
    lowered = detail.lower()
    if any(marker in lowered for marker in _BUSY_MARKERS):
        return PortBusy(port, find_holder(port), detail)
    return TransportUnavailable(f"could not open {port}: {detail}")


class _SerialProtocol(asyncio.Protocol):
    """The asyncio.Protocol bridge between pyserial-asyncio and our `read`/`write`.

    Read backpressure is real rather than advisory: the port is paused when the
    buffered bytes pass a high-water mark and resumed below a low-water mark, so
    an `ESP_LOG` flood cannot grow an unbounded queue in the process that is
    meant to stay responsive.
    """

    HIGH_WATER = 64 * 1024
    LOW_WATER = 8 * 1024

    def __init__(self) -> None:
        self._transport: asyncio.Transport | None = None
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._waiter: asyncio.Future[None] | None = None
        self._closed = False
        self._lost: BaseException | None = None
        self._writing_paused = False

    # asyncio.Protocol -----------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append(data)
        self._buffered += len(data)
        if self._buffered > self.HIGH_WATER and self._transport is not None:
            self._transport.pause_reading()
        self._wake()

    def connection_lost(self, exc: BaseException | None) -> None:
        self._closed = True
        self._lost = exc
        self._wake()

    def pause_writing(self) -> None:
        self._writing_paused = True

    def resume_writing(self) -> None:
        self._writing_paused = False
        self._wake()

    # our surface ----------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self, max_bytes: int) -> bytes:
        while True:
            if self._chunks:
                data = self._chunks.popleft()
                if len(data) <= max_bytes:
                    self._buffered -= len(data)
                    self._resume_if_drained()
                    return data
                head, tail = data[:max_bytes], data[max_bytes:]
                self._chunks.appendleft(tail)
                self._buffered -= len(head)
                self._resume_if_drained()
                return head
            if self._closed:
                if self._lost is not None:
                    raise TransportUnavailable(f"the control link closed: {self._lost}")
                return b""  # a clean EOF; the caller decides what that means
            self._waiter = asyncio.get_running_loop().create_future()
            await self._waiter

    async def drain(self) -> None:
        while self._writing_paused and not self._closed:
            self._waiter = asyncio.get_running_loop().create_future()
            await self._waiter

    def _resume_if_drained(self) -> None:
        if self._buffered < self.LOW_WATER and self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.resume_reading()

    def _wake(self) -> None:
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


class SerialPortTransport(SerialTransport):
    """A `SerialTransport` over the real node, with the §8.3 line discipline.

    `serial_factory` is injectable so the open sequence and the failure
    classification are testable without the device; the default is the real
    `serial.Serial`.
    """

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baud: int = DEFAULT_BAUD,
        *,
        timeout: float = 0.2,
        serial_factory: Callable[..., serial.Serial] | None = None,
    ) -> None:
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._serial_factory = serial_factory or serial.Serial
        self._serial: serial.Serial | None = None
        self._protocol: _SerialProtocol | None = None
        self._transport: _AsyncioSerialTransport | None = None

    @property
    def port(self) -> str:
        return self._port

    @property
    def baud(self) -> int:
        return self._baud

    async def open(self) -> None:
        if self._serial is not None:
            return
        try:
            self._serial = self._serial_factory(
                self._port,
                self._baud,
                timeout=self._timeout,
                dsrdtr=False,
                rtscts=False,
            )
        except (serial.SerialException, OSError) as exc:
            raise _classify(self._port, exc) from exc
        # §8.3: deasserted on open, before any byte is read or written.
        self._deassert()
        protocol = _SerialProtocol()
        self._protocol = protocol
        self._transport = _AsyncioSerialTransport(asyncio.get_running_loop(), protocol, self._serial)

    async def close(self) -> None:
        # §8.3: deasserted again in `finally`, before the node is released.
        self._deassert()
        transport, self._transport = self._transport, None
        self._protocol = None
        self._serial = None
        if transport is not None:
            transport.close()
            # Let pyserial-asyncio finish its close callback (which flushes and
            # closes the underlying Serial) before the fd is reused.
            for _ in range(2):
                await asyncio.sleep(0)

    async def read(self, max_bytes: int) -> bytes:
        protocol = self._protocol
        if protocol is None:
            raise TransportUnavailable(f"{self._port} is not open")
        return await protocol.read(max_bytes)

    async def write(self, data: bytes) -> None:
        protocol, transport = self._protocol, self._transport
        if protocol is None or transport is None:
            raise TransportUnavailable(f"{self._port} is not open")
        transport.write(data)
        await protocol.drain()

    def _deassert(self) -> None:
        serial_port = self._serial
        if serial_port is None:
            return
        for line in (serial_port.setDTR, serial_port.setRTS):
            try:
                line(False)
            except (OSError, serial.SerialException, ValueError):
                # A hot-unplugged node cannot take the line setting; the close
                # path must still complete.
                pass


__all__ = ["PortBusy", "SerialPortTransport", "find_holder"]
