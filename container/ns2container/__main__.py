"""`python -m container.ns2container` — the container process (spec §8.1).

One asyncio process on `python:3.12-slim` serves the HTTP API, the SSE stream
and the static UI, and owns the control link. The CMD in the Dockerfile is this
module; `Settings.from_env()` is the only configuration.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace

from aiohttp import web

from .app import create_app
from .config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ns2container", description="the NS2 controller container")
    parser.add_argument("--host", default=None, help="HTTP bind address (default: $NS2_HTTP_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: $NS2_HTTP_PORT or 8080)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.host or args.port:
        settings = replace(
            settings,
            http_host=args.host or settings.http_host,
            http_port=args.port or settings.http_port,
        )
    app = create_app(settings)
    web.run_app(app, host=settings.http_host, port=settings.http_port, print=None)


if __name__ == "__main__":
    main()
