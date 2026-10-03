"""The device seam's wire vocabulary, as pure values (§2, §3).

Enums here are the **wire numbers**, not display names: `Verb` is the frame's
third header byte, `ErrorCode` is §2.5's 1–9, `EventKind` is §3.3's 1–10. The
`STATUS` and `HELLO` payloads are pinned byte-for-byte by `to_bytes`/
`from_bytes`, so the layout lives in one place and a test can read it.

Nothing here opens a port or imports asyncio; it is the vocabulary the
`device` and `web` seams share.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# ── frame ──────────────────────────────────────────────────────────────────

PROTO_VERSION = 1
MAX_FRAME = 512
CHUNK_SIZE = 256
PLAN_CAPACITY_BYTES = 65536
PLAN_SLOTS = 1


class FrameType(IntEnum):
    """§2.2 — the frame's second header byte."""

    REQUEST = 1
    REPLY = 2
    EVENT = 3


class Verb(IntEnum):
    """§2.4's ten verbs, numbered by their wire value."""

    HELLO = 1
    LOAD_PLAN = 2
    START = 3
    STOP = 4
    STATUS = 5
    PLACE_AMIIBO = 6
    UNPLACE_AMIIBO = 7
    PAIR_UNPAIR = 8
    CONFIG = 9
    ERROR = 10


class ErrorCode(IntEnum):
    """§2.5's closed code set. Zero is `NONE`, `STATUS`-only."""

    NONE = 0
    VER_MISMATCH = 1
    UNKNOWN_TYPE = 2
    UNKNOWN_SUBCMD = 3
    BAD_LENGTH = 4
    BAD_STATE = 5
    NO_PLAN = 6
    ALREADY_RUNNING = 7
    BAD_PLAN = 8
    PLAN_TOO_LARGE = 9


class Mode(IntEnum):
    IDLE = 0
    MACRO = 1
    AMIIBO = 2


class PlanState(IntEnum):
    NONE = 0
    COMMITTED = 1


class TagState(IntEnum):
    NONE = 0
    PLACED = 1


class ConsolePolling(IntEnum):
    IDLE = 0
    POLLING = 1
    TAG_DETECTED = 2


class StopReason(IntEnum):
    """§3.2 — *who* stopped it, a closed set."""

    NONE = 0
    CONTAINER_STOP = 1
    BOOT_LOCAL = 2


class ConsoleLink(IntEnum):
    ADVERTISING = 0
    CONNECTED = 1


class Bond(IntEnum):
    UNPAIRED = 0
    PAIRED = 1


class EventKind(IntEnum):
    """§3.3's ten kinds, numbered by their `kind` byte."""

    MODE_CHANGED = 1
    PLAN_COMMITTED = 2
    PLAN_DISCARDED = 3
    LOOP_COMPLETED = 4
    TAG_PLACED = 5
    TAG_UNPLACED = 6
    SCAN_ENDED = 7
    ERROR_RAISED = 8
    CONSOLE_LINK = 9
    BOOT = 10


class LinkEdge(IntEnum):
    """`CONSOLE_LINK`'s `which` byte (§3.3)."""

    DISCONNECTED = 0
    CONNECTED = 1
    RESUBSCRIBED = 2


# `HELLO.features` bits (§2.6); bits 3–15 are reserved 0.
FEATURE_MACRO = 1 << 0
FEATURE_AMIIBO = 1 << 1
FEATURE_CONFIG = 1 << 2


class Features(IntEnum):
    MACRO = FEATURE_MACRO
    AMIIBO = FEATURE_AMIIBO
    CONFIG = FEATURE_CONFIG


# ── errors ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommandError(Exception):
    """A typed rejection: §2.5's `code` plus its per-code `detail`.

    Raised by whatever implements `DeviceApi` when the device answered with an
    `ERROR` reply. The web seam turns it into a typed HTTP error and the UI
    shows `code` + the per-code sentence; it is never swallowed and never
    retried (`retry the transport, never a mode verb`).
    """

    code: ErrorCode
    detail: int = 0

    ERROR_PAYLOAD = struct.Struct("<BI")

    def to_bytes(self) -> bytes:
        return self.ERROR_PAYLOAD.pack(int(self.code), self.detail)  # type: ignore[arg-type]

    @classmethod
    def from_bytes(cls, payload: bytes) -> "CommandError":
        code, detail = cls.ERROR_PAYLOAD.unpack(payload)
        return cls(ErrorCode(code), detail)

    def __str__(self) -> str:  # pragma: no cover - display only
        name = ErrorCode(self.code).name
        return f"ERROR {name} (detail={self.detail})" if self.detail else f"ERROR {name}"


class ProtocolError(RuntimeError):
    """A frame that arrived intact but is not the shape the spec fixes.

    Distinct from `CommandError`: that is the device's own typed `ERROR` reply
    (§2.5). This is a host-side reading — a `STATUS` that is not 47 bytes, a
    bulk ACK that is not 4, a header `ver` that is not 1 — and the spec says to
    surface it as a protocol error rather than pad, guess, or attribute it to
    the device (§2.6, §2.8, §3.2).
    """


# ── HELLO ──────────────────────────────────────────────────────────────────

_HELLO = struct.Struct("<B4BIHHIBH")


@dataclass(frozen=True, slots=True)
class Hello:
    """§2.6's 20-byte capabilities reply."""

    proto_ver: int = PROTO_VERSION
    fw_major: int = 1
    fw_minor: int = 0
    fw_patch: int = 0
    fw_build: int = 0
    boot_id: int = 0
    max_frame: int = MAX_FRAME
    chunk_size: int = CHUNK_SIZE
    plan_capacity_bytes: int = PLAN_CAPACITY_BYTES
    plan_slots: int = PLAN_SLOTS
    features: int = FEATURE_MACRO | FEATURE_AMIIBO | FEATURE_CONFIG

    @property
    def fw_version(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}.{self.fw_patch}"

    def supports(self, feature: Features) -> bool:
        return bool(self.features & int(feature))

    def to_bytes(self) -> bytes:
        return _HELLO.pack(
            self.proto_ver,
            self.fw_major,
            self.fw_minor,
            self.fw_patch,
            self.fw_build,
            self.boot_id,
            self.max_frame,
            self.chunk_size,
            self.plan_capacity_bytes,
            self.plan_slots,
            self.features,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "Hello":
        return cls(*_HELLO.unpack(payload))


# ── STATUS ─────────────────────────────────────────────────────────────────

_STATUS = struct.Struct("<BBBB16sHHIB7sBBIBI")
STATUS_LENGTH = 47


@dataclass(frozen=True, slots=True)
class Status:
    """§3.2's fixed 47-byte volatile truth.

    `plan_hash` is the 16-byte identity (or 16 zero bytes when
    `plan_state = NONE`); `tag_identity` is the placed tag's 7 bytes.
    """

    console_link: ConsoleLink = ConsoleLink.ADVERTISING
    bond: Bond = Bond.UNPAIRED
    mode: Mode = Mode.IDLE
    plan_state: PlanState = PlanState.NONE
    plan_hash: bytes = bytes(16)
    plan_frame_count: int = 0
    current_frame: int = 0
    loop_count: int = 0
    tag_state: TagState = TagState.NONE
    tag_identity: bytes = bytes(7)
    console_polling: ConsolePolling = ConsolePolling.IDLE
    last_error_code: ErrorCode = ErrorCode.NONE
    last_error_detail: int = 0
    last_stop_reason: StopReason = StopReason.NONE
    uptime_ms: int = 0

    @property
    def plan_identity_hex(self) -> str:
        return self.plan_hash.hex()

    @property
    def tag_identity_hex(self) -> str:
        return self.tag_identity.hex()

    def to_bytes(self) -> bytes:
        payload = _STATUS.pack(
            int(self.console_link),
            int(self.bond),
            int(self.mode),
            int(self.plan_state),
            bytes(self.plan_hash),
            self.plan_frame_count,
            self.current_frame,
            self.loop_count,
            int(self.tag_state),
            bytes(self.tag_identity),
            int(self.console_polling),
            int(self.last_error_code),
            self.last_error_detail,
            int(self.last_stop_reason),
            self.uptime_ms,
        )
        assert len(payload) == STATUS_LENGTH
        return payload

    @classmethod
    def from_bytes(cls, payload: bytes) -> "Status":
        if len(payload) != STATUS_LENGTH:
            # §3.2: anything other than 47 bytes is a protocol error, never padded.
            raise ValueError(f"STATUS is {len(payload)} bytes, expected {STATUS_LENGTH}")
        fields = _STATUS.unpack(payload)
        return cls(
            console_link=ConsoleLink(fields[0]),
            bond=Bond(fields[1]),
            mode=Mode(fields[2]),
            plan_state=PlanState(fields[3]),
            plan_hash=fields[4],
            plan_frame_count=fields[5],
            current_frame=fields[6],
            loop_count=fields[7],
            tag_state=TagState(fields[8]),
            tag_identity=fields[9],
            console_polling=ConsolePolling(fields[10]),
            last_error_code=ErrorCode(fields[11]),
            last_error_detail=fields[12],
            last_stop_reason=StopReason(fields[13]),
            uptime_ms=fields[14],
        )


# ── events ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Event:
    """One `EVENT` frame's decoded payload (§3.3).

    `payload` is exactly the bytes after the `kind` byte, decoded per kind.
    Activities that only need "something changed" read `kind`; the fields below
    are the typed convenience the container uses.
    """

    kind: EventKind
    raw: bytes = b""

    @property
    def mode(self) -> Mode | None:
        return Mode(self.raw[0]) if self.kind is EventKind.MODE_CHANGED else None

    @property
    def plan_hash(self) -> bytes | None:
        return self.raw if self.kind is EventKind.PLAN_COMMITTED else None

    @property
    def loop_count(self) -> int | None:
        if self.kind is not EventKind.LOOP_COMPLETED:
            return None
        return struct.unpack_from("<I", self.raw, 0)[0]

    @property
    def tag_identity(self) -> bytes | None:
        return self.raw if self.kind is EventKind.TAG_PLACED else None

    @property
    def error(self) -> CommandError | None:
        if self.kind is not EventKind.ERROR_RAISED:
            return None
        return CommandError.from_bytes(self.raw)

    @property
    def link_edge(self) -> tuple[LinkEdge, int] | None:
        if self.kind is not EventKind.CONSOLE_LINK:
            return None
        which, reason = struct.unpack_from("<BH", self.raw, 0)
        return LinkEdge(which), reason

    @property
    def boot_id(self) -> int | None:
        if self.kind is not EventKind.BOOT:
            return None
        return struct.unpack_from("<I", self.raw, 0)[0]


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    """The `CONFIG` verb's payload, kept container-side (§2.9)."""

    report_interval_ms: int = 15
    led: bool = True


__all__ = [
    "Bond",
    "CHUNK_SIZE",
    "CommandError",
    "ConsoleConfig",
    "ConsoleLink",
    "ConsolePolling",
    "ErrorCode",
    "Event",
    "EventKind",
    "FEATURE_AMIIBO",
    "FEATURE_CONFIG",
    "FEATURE_MACRO",
    "Features",
    "FrameType",
    "Hello",
    "LinkEdge",
    "MAX_FRAME",
    "Mode",
    "PLAN_CAPACITY_BYTES",
    "PLAN_SLOTS",
    "PROTO_VERSION",
    "PlanState",
    "ProtocolError",
    "STATUS_LENGTH",
    "Status",
    "StopReason",
    "TagState",
    "Verb",
]
