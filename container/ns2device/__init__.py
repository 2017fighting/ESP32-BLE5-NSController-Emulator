"""The device seam (§8.2): the interface that talks to the board, and its stub.

Provisional — the wire vocabulary here is pinned by §2/§3, but the interface is
what issue #30 fills with the real session. Until then `StubDevice` is the only
implementation, and the container is its only caller.
"""

from __future__ import annotations

from .api import DeviceApi, FrameIO, ProgressCallback
from .model import (
    Bond,
    CHUNK_SIZE,
    CommandError,
    ConsoleConfig,
    ConsoleLink,
    ConsolePolling,
    ErrorCode,
    Event,
    EventKind,
    FEATURE_AMIIBO,
    FEATURE_CONFIG,
    FEATURE_MACRO,
    Features,
    FrameType,
    Hello,
    LinkEdge,
    MAX_FRAME,
    Mode,
    PLAN_CAPACITY_BYTES,
    PROTO_VERSION,
    PlanState,
    STATUS_LENGTH,
    Status,
    StopReason,
    TagState,
    Verb,
)
from .stub import TAG_IMAGE_LENGTH, StubDevice
from .session import PlanHashMismatch, SessionDevice

__all__ = [
    "Bond",
    "CHUNK_SIZE",
    "CommandError",
    "ConsoleConfig",
    "ConsoleLink",
    "ConsolePolling",
    "DeviceApi",
    "ErrorCode",
    "Event",
    "EventKind",
    "FEATURE_AMIIBO",
    "FEATURE_CONFIG",
    "FEATURE_MACRO",
    "Features",
    "FrameIO",
    "FrameType",
    "Hello",
    "LinkEdge",
    "MAX_FRAME",
    "Mode",
    "PLAN_CAPACITY_BYTES",
    "PROTO_VERSION",
    "PlanHashMismatch",
    "PlanState",
    "ProgressCallback",
    "STATUS_LENGTH",
    "SessionDevice",
    "Status",
    "StopReason",
    "StubDevice",
    "TAG_IMAGE_LENGTH",
    "TagState",
    "Verb",
]
