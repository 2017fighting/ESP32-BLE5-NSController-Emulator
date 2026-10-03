"""Assembly: the one asyncio process, the four seams and the four screens (§8.1, §8.2).

```text
aiohttp (ns2web)  ──▶  Controller (ns2container.state)  ──DeviceApi──▶  StubDevice
                                                                        (issue #30: the session
                                                                         over ns2serial.FrameIO)
```

The `device` seam is a **stub** in this build by design: ticket #30 replaces it
with the real session against the verbs, and nothing in the web seam or the
state model moves when it does. The sealing seam is the real pure function's
interface; its implementation is ticket #32's, so a placement is refused with a
typed `SEALING_UNAVAILABLE` rather than replaying a stored dump unchanged
(§6.7's one failure mode that looks like success).
"""

from __future__ import annotations

from aiohttp import web

from ..ns2device import DeviceApi, StubDevice
from ..ns2sealing import seal
from ..ns2web import create_web_app
from .config import Settings
from .library import AmiiboIndex, KeyStore, MacroLibrary
from .state import Controller, Sealer

__all__ = ["Controller", "Settings", "create_app"]


def create_controller(
    settings: Settings,
    *,
    device: DeviceApi | None = None,
    sealer: Sealer = seal,
) -> Controller:
    """Build the controller the web seam serves. `device`/`sealer` are injectable for tests."""
    return Controller(
        device=device if device is not None else StubDevice(),
        settings=settings,
        macros=MacroLibrary(settings.macro_dir, capacity_bytes=settings.plan_capacity_bytes),
        figures=AmiiboIndex(settings.amiibo_dir),
        keys=KeyStore(settings.key_file, settings.key_dir),
        sealer=sealer,
    )


def create_app(
    settings: Settings,
    *,
    device: DeviceApi | None = None,
    sealer: Sealer = seal,
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
