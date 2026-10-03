"""Test-side helpers shared by the ns2plan and ns2sealing tests."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "plan"
#: The sealing fixture (fixtures/sealing, §6.4): amiitool's own bytes, no real key.
SEALING_FIXTURE_DIR = REPO_ROOT / "fixtures" / "sealing"

# The reference clones every acceptance row is pinned against (docs/references.md).
REFERENCE_PIN = "202e512206ab955361aa40e8e2a2735cbe142175"
REFERENCE_AMIITOOL_PIN = "4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3"
#: §8.5 — the key is not a figure.
REFERENCE_ESSENTIAL_DIR = "!Essential Files"


def reference_root() -> Path:
    return Path(os.environ.get("REFERENCE_ROOT") or os.path.expanduser("~/clone"))


def reference_macro_dir() -> Path | None:
    """The pinned macro library, or ``None`` when the reference is absent."""
    candidate = reference_root() / "switch-controller-macro" / "宏"
    return candidate if candidate.is_dir() else None


def sealing_fixture(name: str) -> bytes:
    """One fixture file, by stem: `key`, `tag`, `plain`, `trailer`, `identity`, `sealed`."""
    return bytes.fromhex((SEALING_FIXTURE_DIR / f"{name}.hex").read_text().strip())


def reference_amiibo_dir() -> Path | None:
    """The pinned Amiibo clone's `.bin` tree, or ``None`` when it is absent."""
    candidate = reference_root() / "Amiibo" / "Amiibo Bin"
    return candidate if candidate.is_dir() else None


def reference_key_file() -> Path | None:
    """The retail key inside the Amiibo clone, or ``None``: never vendored (§6.7)."""
    directory = reference_amiibo_dir()
    if directory is None:
        return None
    candidate = directory / REFERENCE_ESSENTIAL_DIR / "key_retail.bin"
    return candidate if candidate.is_file() else None


def reference_tag_file(*, minimum: int = 540) -> Path | None:
    """The first `.bin` figure image of at least `minimum` bytes, or ``None``.

    Deterministic (sorted) so a failure names a stable file. `!Essential Files/`
    is skipped: it holds the key and an unrelated archive, not figures (§6.2).
    """
    directory = reference_amiibo_dir()
    if directory is None:
        return None
    for path in sorted(directory.rglob("*.bin")):
        if REFERENCE_ESSENTIAL_DIR in path.parts:
            continue
        if path.stat().st_size >= minimum:
            return path
    return None


def correction_macro_path() -> Path:
    return FIXTURE_DIR / "correction.json"


def correction_plan_hex() -> str:
    return (FIXTURE_DIR / "correction.plan.hex").read_text().strip()


def correction_plan_bytes() -> bytes:
    return bytes.fromhex(correction_plan_hex())


def correction_sha256() -> str:
    return (FIXTURE_DIR / "correction.sha256").read_text().strip()


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# `sys.path` surgery lives here so each test file can import the package the
# same way whether discovery or direct execution put it on the path.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
