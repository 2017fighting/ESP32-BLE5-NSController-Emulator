"""The web seam (§8.2, §8.7): aiohttp routes, one SSE stream, the static UI."""

from __future__ import annotations

from .app import create_web_app

__all__ = ["create_web_app"]
