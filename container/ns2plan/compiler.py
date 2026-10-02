"""The macro compiler (§5), and the plan identity the container owns (§5.6, ADR-0010).

Pure: ``(macro JSON) -> plan bytes + 16-byte identity``. Nothing here opens a
port, touches a file or knows about HTTP, which is what makes the seam testable
without a board and lets the device stay a byte-sink.

Ingestion is all-or-nothing (§5.1): one unrecognised event, name, side, value
shape or out-of-range value rejects the whole macro. The only resampling is the
reference player's own per-side ``< 10 ms`` stick keep-filter (§5.2); buttons
are never filtered. The compiler injects no trailing neutral frame — neutral is
executor-owned (§4.6).
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass
from os import PathLike
from typing import Any, Iterable, Mapping, Sequence

from .format import (
    IDENTITY_LENGTH,
    MAX_HOLD_MS,
    PLAN_FORMAT_VERSION,
    PLAN_HEADER_SIZE,
    PLAN_MAGIC,
    PLAN_RECORD_SIZE,
    payload_size,
)

# §5.1 — button name -> Pro2 register bit, in btn_bits_pro2_t order
# (main/include/controller/hid_controller_pro2.h).
BUTTON_BITS: dict[str, tuple[int, int]] = {}
for _bit, _name in enumerate(("b", "a", "y", "x", "r", "zr", "plus", "r_stick")):
    BUTTON_BITS[_name] = (0, _bit)
for _bit, _name in enumerate(
    ("down", "right", "left", "up", "l", "zl", "minus", "l_stick")
):
    BUTTON_BITS[_name] = (1, _bit)
for _bit, _name in enumerate(("home", "capture", "gr", "gl", "c")):
    BUTTON_BITS[_name] = (2, _bit)
del _bit, _name

STICK_SIDES = ("left", "right")

# §5.2 step 3 — the reference player's per-side stick keep-filter.
STICK_KEEP_INTERVAL_MS = 10.0

# §5.2 step 7 — a stick is centred when both axes are within this of zero.
STICK_CENTRE_EPSILON = 0.01

STICK_CENTRE = 2048
STICK_MIN = 0
STICK_MAX = 4095

_NEUTRAL_STATE = (0, 0, 0, STICK_CENTRE, STICK_CENTRE, STICK_CENTRE, STICK_CENTRE)


class MacroRejected(Exception):
    """The whole macro is refused; nothing is uploaded (§5.1, §5.5).

    ``reason`` is the precise, user-facing sentence the library list shows;
    ``code`` is the short typed tag the UI keys on. ``event_index`` and
    ``t_ms`` locate the offending event when there is one.
    """

    def __init__(
        self,
        reason: str,
        *,
        code: str = "BAD_MACRO",
        event_index: int | None = None,
        t_ms: float | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.event_index = event_index
        self.t_ms = t_ms

    def _located(self) -> str:
        if self.event_index is None:
            return self.reason
        where = f"event {self.event_index}"
        if self.t_ms is not None:
            where += f" (t={self.t_ms:g} ms)"
        return f"{where}: {self.reason}"

    def __str__(self) -> str:  # pragma: no cover - trivia
        return self._located()


class PlanHashMismatch(Exception):
    """The device echoed a hash that disagrees with the one the container sent.

    The container believes neither side silently (§5.6, ADR-0010): it surfaces
    the mismatch instead of rendering the run as current.
    """

    def __init__(self, sent: bytes, echoed: bytes) -> None:
        self.sent = sent
        self.echoed = echoed
        super().__init__(
            "plan hash mismatch: sent "
            f"{sent.hex()} but the device echoed {echoed.hex()}"
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A compiled plan: the exact bytes uploaded and the identity over them."""

    payload: bytes
    record_count: int
    loop_ms: int

    @property
    def identity(self) -> bytes:
        """SHA-256 over the compiled payload, truncated to 16 bytes (§5.6)."""
        return plan_identity(self.payload)

    @property
    def identity_hex(self) -> str:
        return self.identity.hex()

    @property
    def size(self) -> int:
        return len(self.payload)


# A full controller state: 3 button bytes + four u12 stick axes (§5.2 step 2).
State = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _Frame:
    """A plan frame under construction: a state and the ms it starts at."""

    start_ms: int
    state: State


def plan_identity(payload: bytes) -> bytes:
    """The container-owned plan identity: SHA-256 truncated to 16 bytes.

    Hashing the *output* bytes means a compiler or format change yields a new
    identity and forces a re-upload, so a stale plan can never be mistaken for
    current (§5.6, ADR-0010).
    """
    return hashlib.sha256(payload).digest()[:IDENTITY_LENGTH]


def hash_matches(sent: bytes, echoed: bytes) -> bool:
    """Whether the device's echoed hash agrees with the one we sent."""
    return bytes(sent) == bytes(echoed)


def assert_echo(sent: bytes, echoed: bytes) -> None:
    """Raise :class:`PlanHashMismatch` unless the echoed hash agrees."""
    if not hash_matches(sent, echoed):
        raise PlanHashMismatch(bytes(sent), bytes(echoed))


def encode_stick_axis(value: float) -> int:
    """One 12-bit stick axis: ``clamp(round(2048 + clamp(v,-1,1)*2048), 0, 4095)``.

    ``round`` is Python's round-half-to-even; the frozen golden fixture is the
    authority on this and on the centring rule below.
    """
    value = max(-1.0, min(1.0, float(value)))
    return max(STICK_MIN, min(STICK_MAX, round(STICK_CENTRE + value * STICK_CENTRE)))


def encode_stick(h: float, v: float) -> tuple[int, int]:
    """Encode a stick event to its u12 axes, applying the centring rule.

    A stick is centred (both axes 2048) when ``|h| < 0.01 && |v| < 0.01``,
    matching the reference player's apply step (§5.2 step 7).
    """
    if abs(h) < STICK_CENTRE_EPSILON and abs(v) < STICK_CENTRE_EPSILON:
        return STICK_CENTRE, STICK_CENTRE
    return encode_stick_axis(h), encode_stick_axis(v)


def pack_stick_data(x: int, y: int) -> bytes:
    """Pack two u12 axes into the report's 3-byte stick layout (§5.3).

    Mirrors ``pack_stick_data`` in ``main/include/controller/hid_controller.h``.
    """
    x &= 0xFFF
    y &= 0xFFF
    return bytes(
        (
            x & 0xFF,
            ((y & 0x0F) << 4) | ((x >> 8) & 0x0F),
            (y >> 4) & 0xFF,
        )
    )


def _require_number(value: Any, what: str, index: int, t_ms: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MacroRejected(
            f"{what} must be a number, got {type(value).__name__}",
            code="BAD_EVENT",
            event_index=index,
            t_ms=t_ms,
        )
    number = float(value)
    if not math.isfinite(number):
        raise MacroRejected(
            f"{what} must be finite, got {value!r}",
            code="BAD_EVENT",
            event_index=index,
            t_ms=t_ms,
        )
    return number


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    what: str,
    index: int,
    t_ms: float | None,
) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        raise MacroRejected(
            f"{what} has unrecognised key(s): {', '.join(map(str, unknown))}",
            code="BAD_EVENT",
            event_index=index,
            t_ms=t_ms,
        )


def _validate_event(
    entry: Any, index: int, previous_t: float | None
) -> tuple[float, dict[str, Any]]:
    if not isinstance(entry, Mapping):
        raise MacroRejected(
            f"event must be an object, got {type(entry).__name__}",
            code="BAD_EVENT",
            event_index=index,
        )
    _reject_unknown_keys(entry, frozenset(("t", "ev")), "event", index, None)
    if "t" not in entry:
        raise MacroRejected("event is missing 't'", code="BAD_EVENT", event_index=index)
    if "ev" not in entry:
        raise MacroRejected(
            "event is missing 'ev'", code="BAD_EVENT", event_index=index
        )

    t = _require_number(entry["t"], "'t'", index, None)
    if previous_t is not None and t < previous_t:
        raise MacroRejected(
            f"'t' is backward ({t:g} < {previous_t:g}); non-decreasing required",
            code="NON_MONOTONIC",
            event_index=index,
            t_ms=t,
        )

    ev = entry["ev"]
    if not isinstance(ev, Mapping):
        raise MacroRejected(
            f"'ev' must be an object, got {type(ev).__name__}",
            code="BAD_EVENT",
            event_index=index,
            t_ms=t,
        )
    ev_type = ev.get("type")
    if ev_type == "button":
        _reject_unknown_keys(
            ev, frozenset(("type", "name", "pressed")), "'ev'", index, t
        )
        name = ev.get("name")
        if name not in BUTTON_BITS:
            raise MacroRejected(
                f"unknown button name {name!r}",
                code="BAD_EVENT",
                event_index=index,
                t_ms=t,
            )
        if not isinstance(ev.get("pressed"), bool):
            raise MacroRejected(
                "'pressed' must be a boolean",
                code="BAD_EVENT",
                event_index=index,
                t_ms=t,
            )
    elif ev_type == "stick":
        _reject_unknown_keys(
            ev, frozenset(("type", "stick", "h", "v")), "'ev'", index, t
        )
        side = ev.get("stick")
        if side not in STICK_SIDES:
            raise MacroRejected(
                f"unknown stick side {side!r}",
                code="BAD_EVENT",
                event_index=index,
                t_ms=t,
            )
        for axis in ("h", "v"):
            if axis not in ev:
                raise MacroRejected(
                    f"stick event is missing {axis!r}",
                    code="BAD_EVENT",
                    event_index=index,
                    t_ms=t,
                )
            value = _require_number(ev[axis], f"stick {axis!r}", index, t)
            if not -1.0 <= value <= 1.0:
                raise MacroRejected(
                    f"stick {axis!r} is out of range ({value:g}); [-1, 1] required",
                    code="OUT_OF_RANGE",
                    event_index=index,
                    t_ms=t,
                )
    else:
        raise MacroRejected(
            f"unknown event type {ev_type!r}",
            code="BAD_EVENT",
            event_index=index,
            t_ms=t,
        )
    return t, dict(ev)


def _apply_event(state: State, ev: Mapping[str, Any]) -> State:
    buttons = [state[0], state[1], state[2]]
    left = [state[3], state[4]]
    right = [state[5], state[6]]
    if ev["type"] == "button":
        byte, bit = BUTTON_BITS[ev["name"]]
        if ev["pressed"]:
            buttons[byte] |= 1 << bit
        else:
            buttons[byte] &= ~(1 << bit)
    else:
        side = ev["stick"]
        axes = encode_stick(float(ev["h"]), float(ev["v"]))
        if side == "left":
            left = list(axes)
        else:
            right = list(axes)
    return (
        buttons[0],
        buttons[1],
        buttons[2],
        left[0],
        left[1],
        right[0],
        right[1],
    )


def compile_macro(
    events: Sequence[Mapping[str, Any]],
    *,
    capacity_bytes: int | None = None,
) -> Plan:
    """Compile canonical macro events to a plan (§5.2), or reject the macro.

    Parameters
    ----------
    events:
        The canonical macro: a sequence of ``{"t": float, "ev": {...}}``.
    capacity_bytes:
        ``HELLO.plan_capacity_bytes``. When given, the compiled payload is
        pre-checked against it so ``PLAN_TOO_LARGE`` is a container-side
        refusal (``PLAN_TOO_LARGE``) rather than the device's first hint
        (§8.4). The device's own check remains the backstop.
    """
    if (
        not isinstance(events, Sequence)
        or isinstance(events, (str, bytes))
        or len(events) == 0
    ):
        raise MacroRejected("macro is empty", code="EMPTY")

    validated: list[tuple[float, dict[str, Any]]] = []
    previous_t: float | None = None
    for index, entry in enumerate(events):
        t, ev = _validate_event(entry, index, previous_t)
        validated.append((t, ev))
        previous_t = t

    t_first = validated[0][0]
    loop_ms = round(validated[-1][0] - t_first)

    state: State = _NEUTRAL_STATE
    # §5.2: a plan starts from the initial state at t0 = t_first. Seeding that
    # first frame keeps a macro whose first change lands later — or that never
    # changes at all — from losing its leading span; a change at t0 merges into
    # it (start 0), which leaves the four real macros byte-identical.
    frames: list[_Frame] = [_Frame(0, _NEUTRAL_STATE)]
    last_kept: dict[str, float] = {side: -math.inf for side in STICK_SIDES}

    for t, ev in validated:
        if ev["type"] == "stick":
            side = ev["stick"]
            # §5.2 step 3: applied only if t - last_kept[side] >= 10; the
            # counter updates only on keep. Buttons are never filtered.
            if t - last_kept[side] < STICK_KEEP_INTERVAL_MS:
                continue
            last_kept[side] = t

        new_state = _apply_event(state, ev)
        if new_state == state:
            continue  # §5.2 step 4: no-op suppression, folded into the hold

        start = round(t - t_first)
        if start == frames[-1].start_ms:
            # §5.2 step 5: same rounded millisecond merges into the later state.
            frames[-1] = _Frame(start, new_state)
            # If the merge lands back on the state before it, the two changes
            # cancel and both fold away (§5.2 step 4: one record per distinct
            # state, no zero-length middle frame).
            if len(frames) >= 2 and frames[-2].state == new_state:
                frames.pop()
        else:
            frames.append(_Frame(start, new_state))
        state = new_state

    header = struct.pack(
        "<IBBHI",
        PLAN_MAGIC,
        PLAN_FORMAT_VERSION,
        PLAN_RECORD_SIZE,
        len(frames),
        loop_ms,
    )
    chunks = [header]
    total_hold = 0
    for index, frame in enumerate(frames):
        next_start = frames[index + 1].start_ms if index + 1 < len(frames) else loop_ms
        hold = next_start - frame.start_ms
        if hold > MAX_HOLD_MS:
            raise MacroRejected(
                f"single hold is {hold} ms, above the u16 ceiling",
                code="HOLD_TOO_LONG",
            )
        total_hold += hold
        buttons = bytes(frame.state[0:3])
        left = pack_stick_data(frame.state[3], frame.state[4])
        right = pack_stick_data(frame.state[5], frame.state[6])
        chunks.append(buttons + left + right + struct.pack("<H", hold))

    # §5.5 self-check: sum(hold) == round(t_last - t_first).
    if total_hold != loop_ms:
        raise AssertionError(
            f"duration mismatch: sum(hold)={total_hold} but loop_ms={loop_ms}"
        )

    payload = b"".join(chunks)
    if len(payload) != payload_size(len(frames)):  # pragma: no cover - invariant
        raise AssertionError("plan payload length does not match record count")
    if capacity_bytes is not None and len(payload) > capacity_bytes:
        raise MacroRejected(
            f"plan is {len(payload)} bytes, above the device capacity of "
            f"{capacity_bytes}",
            code="PLAN_TOO_LARGE",
        )
    return Plan(payload=payload, record_count=len(frames), loop_ms=loop_ms)


def compile_json(raw: str | bytes, *, capacity_bytes: int | None = None) -> Plan:
    """Parse a canonical macro JSON document and compile it."""
    try:
        events = json.loads(raw)
    except ValueError as exc:
        raise MacroRejected(f"macro is not valid JSON: {exc}", code="BAD_JSON") from exc
    return compile_macro(events, capacity_bytes=capacity_bytes)


def load_plan(path: str | PathLike[str], *, capacity_bytes: int | None = None) -> Plan:
    """Read and compile a macro file. A rejected macro raises ``MacroRejected``.

    The library scanner calls this so a rejected macro still keeps its reason
    visible in the list (§8.4).
    """
    with open(path, encoding="utf-8") as handle:
        return compile_json(handle.read(), capacity_bytes=capacity_bytes)


def iter_frames(plan: Plan) -> Iterable[tuple[State, int]]:
    """Decode ``(state, hold_ms)`` plan frames from a compiled plan.

    The inverse of the frame writer: ``state`` is
    ``(b0, b1, b2, lx, ly, rx, ry)``. A *plan frame*, never an "event"
    (CONTEXT.md); used by tests and by the frame trace.
    """
    header = struct.unpack_from("<IBBHI", plan.payload, 0)
    magic, version, record_size, record_count, loop_ms = header
    if magic != PLAN_MAGIC:
        raise ValueError("not an NSPL plan")
    if version != PLAN_FORMAT_VERSION or record_size != PLAN_RECORD_SIZE:
        raise ValueError("unsupported plan header")
    if len(plan.payload) != payload_size(record_count):
        raise ValueError("plan payload length does not match record count")
    offset = PLAN_HEADER_SIZE
    for _ in range(record_count):
        record = plan.payload[offset : offset + PLAN_RECORD_SIZE]
        lx = record[3] | ((record[4] & 0x0F) << 8)
        ly = ((record[4] >> 4) & 0x0F) | (record[5] << 4)
        rx = record[6] | ((record[7] & 0x0F) << 8)
        ry = ((record[7] >> 4) & 0x0F) | (record[8] << 4)
        hold = struct.unpack_from("<H", record, 9)[0]
        yield (record[0], record[1], record[2], lx, ly, rx, ry), hold
        offset += PLAN_RECORD_SIZE
