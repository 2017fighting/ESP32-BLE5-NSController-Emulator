#!/usr/bin/env python3
"""The port seam: the §8.3 DTR/RTS sequence and `Port busy` classification (#30).

The board cannot be relied on in CI, so the open sequence is asserted against a
fake `serial.Serial` and a fake asyncio transport: what is pinned is the order
(deassert *after* construction and again before close, never asserted on open)
and the failure taxonomy (`Port busy` is distinct from "no device" so the
container can refuse to retry through a held port, §8.9).

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2serial_port*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import serial  # noqa: E402

from container.ns2serial import PortBusy, SerialPortTransport, TransportUnavailable  # noqa: E402
from container.ns2serial import port as port_module  # noqa: E402


class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.port = None
        self.rts = True
        self.dtr = True

    def open(self) -> None:
        self.calls.append(("open", self.port))

    def setDTR(self, value: bool) -> None:
        self.calls.append(("DTR", value))

    def setRTS(self, value: bool) -> None:
        self.calls.append(("RTS", value))

    def close(self) -> None:
        self.calls.append(("close", None))

    def fileno(self) -> int:
        return -1


class FakeAsyncioTransport:
    instances: ClassVar[list[FakeAsyncioTransport]] = []

    def __init__(self, loop, protocol, serial_port):
        self.loop = loop
        self.protocol = protocol
        self.serial_port = serial_port
        self.closed = False
        type(self).instances.append(self)

    def write(self, data: bytes) -> None:
        self.serial_port.calls.append(("write", bytes(data)))

    def get_write_buffer_size(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


class PortSequenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncioTransport.instances = []

    def build(self, factory=None) -> tuple[SerialPortTransport, FakeSerial]:
        fake = FakeSerial()

        def default_factory(*args, **kwargs):
            fake.args = args
            fake.kwargs = kwargs
            return fake

        port = SerialPortTransport(
            "/dev/ttyACM0", 921600, serial_factory=factory or default_factory
        )
        return port, fake

    async def test_open_is_deasserted_before_the_port_opens(self):
        port, fake = self.build()
        with mock.patch.object(port_module, "_AsyncioSerialTransport", FakeAsyncioTransport):
            await port.open()
        # Constructed closed (no port, so pyserial cannot raise RTS/DTR), the
        # line state applied while still closed, and only then opened on the
        # real node — §8.3's "deasserted on open" made literal, because RTS is
        # EN on this board and an asserted open is a reset pulse (#36).
        self.assertEqual(fake.args, (None, 921600))
        self.assertEqual(fake.kwargs["dsrdtr"], False)
        self.assertEqual(fake.kwargs["rtscts"], False)
        self.assertEqual((fake.rts, fake.dtr), (False, False))
        self.assertEqual(fake.port, "/dev/ttyACM0")
        self.assertEqual(fake.calls, [("open", "/dev/ttyACM0"), ("DTR", False), ("RTS", False)])
        await port.close()

    async def test_close_deasserts_again_before_releasing_the_node(self):
        port, fake = self.build()
        with mock.patch.object(port_module, "_AsyncioSerialTransport", FakeAsyncioTransport):
            await port.open()
            fake.calls.clear()
            await port.close()
        self.assertEqual(fake.calls, [("DTR", False), ("RTS", False)])
        self.assertEqual(len(FakeAsyncioTransport.instances), 1)
        self.assertTrue(FakeAsyncioTransport.instances[0].closed)

    async def test_open_is_idempotent(self):
        created = []
        port, fake = self.build()

        def factory(*args, **kwargs):
            created.append(args)
            fake.args = args
            fake.kwargs = kwargs
            return fake

        port = SerialPortTransport("/dev/ttyACM0", 921600, serial_factory=factory)
        with mock.patch.object(port_module, "_AsyncioSerialTransport", FakeAsyncioTransport):
            await port.open()
            await port.open()
        self.assertEqual(len(created), 1)
        await port.close()

    async def test_a_held_port_is_port_busy_and_never_retried_through(self):
        def factory(*args, **kwargs):
            raise serial.SerialException("Resource busy")

        port, _ = self.build(factory)
        with (
            mock.patch.object(port_module, "find_holder", return_value="pid 4242"),
            self.assertRaises(PortBusy) as caught,
        ):
            await port.open()
        self.assertEqual(caught.exception.holder, "pid 4242")
        self.assertEqual(caught.exception.port, "/dev/ttyACM0")

    async def test_an_absent_port_is_not_port_busy(self):
        def factory(*args, **kwargs):
            raise serial.SerialException("No such file or directory")

        port, _ = self.build(factory)
        with self.assertRaises(TransportUnavailable) as caught:
            await port.open()
        self.assertNotIsInstance(caught.exception, PortBusy)


if __name__ == "__main__":
    unittest.main()
