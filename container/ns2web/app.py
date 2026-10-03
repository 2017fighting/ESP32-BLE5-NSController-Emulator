"""The web seam (§8.2, §8.7): routes, SSE and the static UI.

It **never opens the port and never sees key bytes**: every handler calls the
`Controller`, which is where device truth lives. §8.7's three surfaces are the
whole API:

| Surface | Shape |
| --- | --- |
| `GET /api/state` | `{"state": <UiState>, "logs": [...]}` — sent whole |
| `GET /api/events` | one SSE stream of `state` snapshots and `log` lines |
| `POST /api/<verb>` | one endpoint per verb, returning the state or a typed error |

`POST` is the container's verb surface, not a wire verb per endpoint: the
Control screen's `Start macro` is `load_plan` + `start` and is one call, while
`Rescan`, `Reconnect` and `Clear logs` are container-local and never reach the
board.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

from ..ns2device import CommandError
from ..ns2container.state import Controller, ControllerError, describe_error

#: Typed app keys (aiohttp 3.9+); plain strings warn at startup.
CONTROLLER_KEY = web.AppKey("controller", Controller)
STATIC_DIR_KEY = web.AppKey("static_dir", Path)

STATE_UNAVAILABLE = web.json_response(
    {"error": {"code": "NO_DEVICE", "message": "the controller is not running"}},
    status=503,
)


def _error_response(code: str, message: str, *, detail: int = 0, status: int = 409) -> web.Response:
    error: dict = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    return web.json_response({"error": error}, status=status)


def _controller(request: web.Request) -> Controller:
    controller: Controller | None = request.app.get(CONTROLLER_KEY)
    if controller is None:  # pragma: no cover - guarded by create_app
        raise web.HTTPServiceUnavailable(text="no controller")
    return controller


async def _read_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="request body must be a JSON object") from None
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return payload


async def get_state(request: web.Request) -> web.Response:
    controller = _controller(request)
    return web.json_response({"state": controller.snapshot(), "logs": controller.logs()})


async def get_events(request: web.Request) -> web.StreamResponse:
    controller = _controller(request)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    try:
        async for name, payload in controller.subscribe():
            block = f"event: {name}\ndata: {json.dumps(payload)}\n\n"
            await response.write(block.encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def _run(request: web.Request, action) -> web.Response:
    controller = _controller(request)
    try:
        await action(controller, request)
    except CommandError as error:
        return _error_response(
            error.code.name,
            describe_error(error.code, error.detail),
            detail=error.detail,
        )
    except ControllerError as error:
        return _error_response(error.code, error.message)
    except web.HTTPException:
        raise
    return web.json_response({"state": controller.snapshot()})


async def post_start(request: web.Request) -> web.Response:
    body = await _read_body(request)
    macro_id = str(body.get("macroId") or "")
    if not macro_id:
        raise web.HTTPBadRequest(text="macroId is required")
    return await _run(request, lambda c, _r: c.start_macro(macro_id))


async def post_stop(request: web.Request) -> web.Response:
    return await _run(request, lambda c, _r: c.stop_macro())


async def post_cancel(request: web.Request) -> web.Response:
    controller = _controller(request)
    await controller.cancel_upload()
    return web.json_response({"state": controller.snapshot()})


async def post_place(request: web.Request) -> web.Response:
    body = await _read_body(request)
    figure_id = str(body.get("figureId") or "")
    if not figure_id:
        raise web.HTTPBadRequest(text="figureId is required")
    return await _run(request, lambda c, _r: c.place_figure(figure_id))


async def post_unplace(request: web.Request) -> web.Response:
    return await _run(request, lambda c, _r: c.unplace())


async def post_pair_unpair(request: web.Request) -> web.Response:
    return await _run(request, lambda c, _r: c.pair_unpair())


async def post_config(request: web.Request) -> web.Response:
    body = await _read_body(request)
    try:
        interval = int(body.get("reportIntervalMs"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="reportIntervalMs must be an integer") from None
    led = bool(body.get("led", True))
    return await _run(request, lambda c, _r: c.save_config(report_interval_ms=interval, led=led))


async def post_rescan(request: web.Request) -> web.Response:
    return await _run(request, lambda c, _r: c.rescan())


async def post_reconnect(request: web.Request) -> web.Response:
    return await _run(request, lambda c, _r: c.reconnect())


async def post_clear_logs(request: web.Request) -> web.Response:
    controller = _controller(request)
    controller.clear_logs()
    return web.json_response({"state": controller.snapshot()})


async def get_index(request: web.Request) -> web.Response:
    static_dir = request.app[STATIC_DIR_KEY]
    index = static_dir / "index.html"
    if not index.is_file():
        return web.Response(
            status=503,
            text=(
                "The UI has not been built. Run `npm ci && npm run build` in container/web, "
                "or build the image (spec §8.1).\n"
            ),
        )
    return web.FileResponse(index)


async def get_healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_web_app(controller: Controller, *, static_dir: Path) -> web.Application:
    app = web.Application()
    app[CONTROLLER_KEY] = controller
    app[STATIC_DIR_KEY] = Path(static_dir)

    app.router.add_get("/healthz", get_healthz)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/events", get_events)
    app.router.add_post("/api/start", post_start)
    app.router.add_post("/api/stop", post_stop)
    app.router.add_post("/api/cancel", post_cancel)
    app.router.add_post("/api/place", post_place)
    app.router.add_post("/api/unplace", post_unplace)
    app.router.add_post("/api/pair_unpair", post_pair_unpair)
    app.router.add_post("/api/config", post_config)
    app.router.add_post("/api/rescan", post_rescan)
    app.router.add_post("/api/reconnect", post_reconnect)
    app.router.add_post("/api/logs/clear", post_clear_logs)

    app.router.add_get("/", get_index)
    assets = Path(static_dir) / "assets"
    if assets.is_dir():
        app.router.add_static("/assets", assets, name="assets", follow_symlinks=False)
    return app


__all__ = ["create_web_app"]
