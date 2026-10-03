"""Assembly: the one asyncio process, the four seams and the four screens (§8.1, §8.2).

```text
aiohttp (ns2web)  ──▶  Controller (ns2container.state)  ──DeviceApi──▶  SessionDevice
                                                                        └ FrameTransport
                                                                          └ SerialPortTransport
```

The `device` seam is the **real session**: `ns2serial.SerialPortTransport` is
the port (with the §8.3 DTR/RTS sequence), `ns2serial.FrameTransport` is the
§2.3 conversation and the §2.7 bulk window, and `ns2device.SessionDevice` is
the verbs. `StubDevice` remains for tests that want the §4.3 transition table
without a wire. The sealing seam is the real port (`ns2sealing.Sealer`, with
§6.4's lazy plaintext cache), and the key store is given the amiibo index so
§6.7's rung 3 can verify the key against the mounted library.
"""

from __future__ import annotations

from aiohttp import web

from ..ns2device import DeviceApi, SessionDevice
from ..ns2sealing import Sealer
from ..ns2serial import FrameTransport, SerialPortTransport
from ..ns2web import create_web_app
from .config import Settings
from .library import AmiiboIndex, KeyStore, MacroLibrary
from .state import Controller, SealingSeam

__all__ = ["Controller", "Settings", "create_app"]


def create_device(settings: Settings) -> DeviceApi:
    """The real device session over the configured port (§8.2, §8.3)."""
    return SessionDevice(FrameTransport(SerialPortTransport(settings.port, settings.baud)))


def create_controller(
    settings: Settings,
    *,
    device: DeviceApi | None = None,
    sealer: SealingSeam | None = None,
) -> Controller:
    """Build the controller the web seam serves. `device`/`sealer` are injectable for tests."""
    figures = AmiiboIndex(settings.amiibo_dir)
    return Controller(
        device=device if device is not None else create_device(settings),
        settings=settings,
        macros=MacroLibrary(settings.macro_dir, capacity_bytes=settings.plan_capacity_bytes),
        figures=figures,
        keys=KeyStore(settings.key_file, settings.key_dir, index=figures),
        sealer=sealer if sealer is not None else Sealer(),
    )


def create_app(
    settings: Settings,
    *,
    device: DeviceApi | None = None,
    sealer: SealingSeam | None = None,
) -> web.Application:
    """The aiohttp application, with the controller's lifecycle wired in."""
    controller = create_controller(settings, device=device, sealer=sealer)
    app = create_web_app(controller, static_dir=settings.static_dir)

    async def _startup(_app: web.Application) -> None:
        await controller.start()

    async def _cleanup(_app: web.Application) -> None:
        await controller.stop()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app
