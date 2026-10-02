"""Test-side helpers shared by the ns2plan tests."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "plan"

# The reference clone every acceptance row is pinned against (docs/references.md).
REFERENCE_PIN = "202e512206ab955361aa40e8e2a2735cbe142175"


def reference_root() -> Path:
    return Path(os.environ.get("REFERENCE_ROOT") or os.path.expanduser("~/clone"))


def reference_macro_dir() -> Path | None:
    """The pinned macro library, or ``None`` when the reference is absent."""
    candidate = reference_root() / "switch-controller-macro" / "宏"
    return candidate if candidate.is_dir() else None


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
