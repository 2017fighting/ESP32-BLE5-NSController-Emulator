"""Where the process is configured (§10.4, §10.5, §10.7).

The port is configuration with `/dev/ttyACM0` as the documented default, so a
replugged board can be pinned by `/dev/serial/by-id` on Linux; on macOS the
node is forwarded onto that same container path by the compose override
(ADR-0014). The three mounts are fixed by §10.4 — the key's path especially:
it is fixed rather than configurable so the locked state can name it truthfully
(ADR-0012). The `NS2_*` overrides exist for tests and for a host run without
mounts; the container itself needs none of them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..ns2device import PLAN_CAPACITY_BYTES
from ..ns2serial import DEFAULT_BAUD, DEFAULT_PORT, FALLBACK_BAUD

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
DEFAULT_STATIC_DIR = WEB_ROOT / "dist"


@dataclass(frozen=True, slots=True)
class Settings:
    port: str = DEFAULT_PORT
    baud: int = DEFAULT_BAUD
    fallback_baud: int = FALLBACK_BAUD
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    macro_dir: Path = Path("/library/macros")
    amiibo_dir: Path = Path("/library/amiibo")
    key_file: Path = Path("/keys/key_retail.bin")
    key_dir: Path = Path("/keys")
    plan_capacity_bytes: int = PLAN_CAPACITY_BYTES
    status_hz: float = 2.0
    log_capacity: int = 400
    static_dir: Path = DEFAULT_STATIC_DIR

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def path(name: str, default: Path) -> Path:
            value = source.get(name)
            return Path(value) if value else default

        return cls(
            port=source.get("CONTROLLER_PORT") or DEFAULT_PORT,
            baud=int(source.get("CONTROLLER_BAUD") or DEFAULT_BAUD),
            http_host=source.get("NS2_HTTP_HOST") or "0.0.0.0",
            http_port=int(source.get("NS2_HTTP_PORT") or 8080),
            macro_dir=path("NS2_MACRO_DIR", Path("/library/macros")),
            amiibo_dir=path("NS2_AMIIBO_DIR", Path("/library/amiibo")),
            key_file=path("NS2_KEY_FILE", Path("/keys/key_retail.bin")),
            key_dir=path("NS2_KEY_DIR", Path("/keys")),
            static_dir=path("NS2_STATIC_DIR", DEFAULT_STATIC_DIR),
        )


__all__ = ["DEFAULT_STATIC_DIR", "Settings", "WEB_ROOT"]
