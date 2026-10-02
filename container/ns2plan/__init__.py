"""``ns2plan`` — the plan compiler and its container-owned identity (spec §5).

Pure and dependency-free: importing this package must not import ``aiohttp``,
``serial`` or anything that opens a port, so the compiler can be tested, and
reused by the device session, without a bench.
"""

from __future__ import annotations

from .cache import PlanCache, PlanCacheEntry
from .compiler import (
    BUTTON_BITS,
    STICK_SIDES,
    MacroRejected,
    Plan,
    PlanHashMismatch,
    State,
    assert_echo,
    compile_json,
    compile_macro,
    encode_stick,
    encode_stick_axis,
    hash_matches,
    iter_frames,
    load_plan,
    pack_stick_data,
    plan_identity,
)
from .format import (
    IDENTITY_LENGTH,
    PLAN_FORMAT_VERSION,
    PLAN_HEADER_SIZE,
    PLAN_MAGIC,
    PLAN_MAGIC_BYTES,
    PLAN_RECORD_SIZE,
    payload_size,
)

__all__ = [
    "BUTTON_BITS",
    "IDENTITY_LENGTH",
    "MacroRejected",
    "PLAN_FORMAT_VERSION",
    "PLAN_HEADER_SIZE",
    "PLAN_MAGIC",
    "PLAN_MAGIC_BYTES",
    "PLAN_RECORD_SIZE",
    "Plan",
    "PlanCache",
    "PlanCacheEntry",
    "PlanHashMismatch",
    "STICK_SIDES",
    "State",
    "assert_echo",
    "compile_json",
    "compile_macro",
    "encode_stick",
    "encode_stick_axis",
    "hash_matches",
    "iter_frames",
    "load_plan",
    "pack_stick_data",
    "payload_size",
    "plan_identity",
]
