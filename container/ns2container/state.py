"""The container's state model: device truth + container facts → one UI state (§8.7).

This is the module the web seam calls. It is deliberately *above* `DeviceApi`:
the web seam never touches the port, and this is the one place device truth is
converted into the state the browser renders (§8.2's "the browser never talks
to the device"). Issue #30 swapped the `StubDevice` for the real session without
moving any of this; chapter 9's console-lifecycle policy lives in
`_on_console_link`/`_on_scan_ended` (#31); issue #32 replaces the injected
`sealer`.

Two rules from §8.10 are structural here:

- **No optimistic mode change.** `mode`, `plan` and `placement` are read from
  `STATUS`, never from what a verb was asked to do.
- **Every event is a prompt to re-read `STATUS`, never a substitute for it**
  (§3.1). `_on_event` sets one local fact at most and then refreshes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from ..ns2device import (
    CommandError,
    ConsoleConfig,
    ConsoleLink,
    DeviceApi,
    ErrorCode,
    Event,
    EventKind,
    Features,
    Hello,
    LinkEdge,
    Mode,
    PlanHashMismatch,
    PlanState,
    ProtocolError,
    Status,
    StopReason,
    TagState,
)
from ..ns2sealing import KeyInvalid, KeyMaterial, SealedTag, TagInvalid
from ..ns2serial import PortBusy, TransportUnavailable
from .config import Settings
from .library import AmiiboIndex, KeyState, KeyStatus, KeyStore, MacroEntry, MacroLibrary

#: The sealing seam's shape (§8.2), injected so tests can substitute a double
#: and the controller never depends on the sealer's implementation. `identity`
#: pins the seal to a given UID; a fixed identity is what the bench's
#: "present the same amiibo again" step needs (§6.6, the reference's
#: continue-for-write), and production always leaves it `None` so every
#: placement mints a fresh one (ADR-0011).
class SealingSeam(Protocol):
    def __call__(
        self, tag_image: bytes, key: KeyMaterial, *, identity: bytes | None = None
    ) -> SealedTag: ...

logger = logging.getLogger("ns2.container")
logger.addHandler(logging.NullHandler())  # no lastResort noise until a host configures logging
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}

ERROR_SENTENCES: dict[ErrorCode, str] = {
    ErrorCode.VER_MISMATCH: "The board speaks another protocol version.",
    ErrorCode.UNKNOWN_TYPE: "The board did not understand a frame type.",
    ErrorCode.UNKNOWN_SUBCMD: "The board did not understand a verb.",
    ErrorCode.BAD_LENGTH: "The board rejected the frame's length.",
    ErrorCode.BAD_STATE: "That is not legal in the mode the board is in.",
    ErrorCode.NO_PLAN: "No plan is committed — upload the macro first.",
    ErrorCode.ALREADY_RUNNING: "The board is already running a plan.",
    ErrorCode.BAD_PLAN: "The board refused the committed plan as unrunnable.",
    ErrorCode.PLAN_TOO_LARGE: "The plan is larger than the board will accept.",
}


def describe_error(code: ErrorCode, detail: int) -> str:
    sentence = ERROR_SENTENCES.get(code, "The board rejected the request.")
    if code is ErrorCode.PLAN_TOO_LARGE and detail:
        return f"{sentence} It is {detail} bytes."
    if code is ErrorCode.BAD_STATE and detail:
        try:
            return f"{sentence} The board is in {Mode(detail).name}."
        except ValueError:
            pass
    return sentence


class ControllerError(Exception):
    """A refusal decided by the container, before anything reaches the wire (§8.6)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LogLine:
    id: int
    t: float
    source: str  # container | device | frame
    level: str  # debug | info | warn | error
    message: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "t": self.t,
            "source": self.source,
            "level": self.level,
            "message": self.message,
        }


class Controller:
    """One process's view of one device, plus the container facts beside it."""

    def __init__(
        self,
        *,
        device: DeviceApi,
        settings: Settings,
        macros: MacroLibrary,
        figures: AmiiboIndex,
        keys: KeyStore,
        sealer: SealingSeam,
    ) -> None:
        self.device = device
        self.settings = settings
        self.macros = macros
        self.figures = figures
        self.keys = keys
        self._sealer = sealer

        self._started_at = time.monotonic()
        self._hello: Hello | None = None
        self._status: Status | None = None
        self._key_status: KeyStatus = KeyStatus(KeyState.KEY_ABSENT)
        self._config = ConsoleConfig()

        self._control_link = "DOWN"
        self._held_by: str | None = None

        self._committed_macro_id: str | None = None
        self._committed_bytes = 0
        self._uploading: dict | None = None
        self._cancel_requested = False
        self._placed_figure_id: str | None = None
        self._placement_index = 0
        self._placed_at: float | None = None
        self._scans = 0
        self._last_polling = 0

        self._console_lost = False
        self._last_drop_reason: str | None = None
        self._recovery = "SAME_POWER"
        # §9.3: a drop was observed while a tag was placed — the next up-edge
        # rotates the placement so the console cannot re-scan an identity it
        # already saw. Not "rotate on every connect": a first connect scans a
        # virgin placement and needs nothing.
        self._rotate_pending = False

        self._logs: deque[LogLine] = deque(maxlen=settings.log_capacity)
        self._log_seq = 0
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._closing = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.log("container", "info", f"control: opening {self.settings.port} @ {self.settings.baud} (dsrdtr=False, rtscts=False, DTR/RTS deasserted)")
        self._scan_libraries()
        self._key_status = self.keys.read()
        self.log("container", "info", self._key_line())
        await self._attempt_connect()
        self._tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._event_loop()),
            asyncio.create_task(self._log_loop()),
            asyncio.create_task(self._connect_loop()),
        ]
        # One loop turn so the event subscription is registered before any verb
        # flows. Events are hints (§3.1), but a hint dropped because nobody was
        # listening yet is still a bug worth not having.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        with contextlib.suppress(Exception):
            await self.device.close()

    # ── reads ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        status = self._status
        hello = self._hello
        firmware = None
        if hello is not None:
            firmware = {
                "protoVer": hello.proto_ver,
                "fwVersion": hello.fw_version,
                "bootId": f"{hello.boot_id:08x}",
                "maxFrame": hello.max_frame,
                "chunkSize": hello.chunk_size,
                "planCapacityBytes": hello.plan_capacity_bytes,
                "features": {
                    "macro": hello.supports(Features.MACRO),
                    "amiibo": hello.supports(Features.AMIIBO),
                    "config": hello.supports(Features.CONFIG),
                },
            }

        plan = None
        placement = None
        mode = "IDLE"
        console_link = "ADVERTISING"
        bonded = False
        stop_reason = "NONE"
        last_error = None
        if status is not None:
            mode = status.mode.name
            console_link = status.console_link.name
            bonded = bool(status.bond)
            if status.plan_state is PlanState.COMMITTED:
                plan = {
                    "macroId": self._committed_macro_id,
                    "hash": status.plan_identity_hex,
                    "frameCount": status.plan_frame_count,
                    "currentFrame": status.current_frame,
                    "loopCount": status.loop_count,
                    "bytes": self._committed_bytes,
                }
            if status.tag_state is TagState.PLACED:
                placement = {
                    "figureId": self._placed_figure_id,
                    "identity": status.tag_identity_hex,
                    "index": self._placement_index,
                    "scans": self._scans,
                    "sinceSec": max(0.0, time.monotonic() - (self._placed_at or time.monotonic())),
                }
            if status.last_error_code is not ErrorCode.NONE:
                last_error = {
                    "code": status.last_error_code.name,
                    "message": describe_error(status.last_error_code, status.last_error_detail),
                }
            stop_reason = self._stop_reason(status)

        return {
            "control": {
                "link": self._control_link,
                "port": self.settings.port,
                "baud": self.settings.baud,
                "heldBy": self._held_by,
            },
            "console": {
                "link": console_link,
                "bonded": bonded,
                "lastDropReason": self._last_drop_reason,
            },
            "firmware": firmware,
            "mode": mode,
            "plan": plan,
            "uploading": self._uploading,
            "placement": placement,
            "key": self._key_status.state.value,
            "keySpelling": self._key_status.spelling if self._key_status.material else None,
            "macroLibrary": "EMPTY" if self.macros.empty else "READY",
            "amiiboLibrary": "EMPTY" if self.figures.empty else "READY",
            "macros": [self._macro_json(entry) for entry in self.macros.entries],
            "figures": [
                {"id": e.id, "name": e.name, "series": e.series, "source": e.source}
                for e in self.figures.entries
            ],
            "series": self.figures.series,
            "stopReason": stop_reason,
            "lastError": last_error,
            "recovery": self._recovery,
            "config": {"reportIntervalMs": self._config.report_interval_ms, "led": self._config.led},
        }

    def logs(self) -> list[dict]:
        return [line.to_dict() for line in self._logs]

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        """SSE events: `("state", <UiState>)` and `("log", <LogLine>)`."""
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield "state", self.snapshot()
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    # ── verbs (§8.2's DeviceApi, reached through the container) ────────────

    async def start_macro(self, macro_id: str) -> None:
        entry = self.macros.resolve(macro_id)
        if entry is None:
            raise ControllerError("UNKNOWN_MACRO", f"No macro with id {macro_id!r} is indexed.")
        if not entry.runnable or entry.plan is None:
            reason = entry.rejection.message if entry.rejection else "the macro was rejected"
            raise ControllerError("MACRO_REJECTED", reason)
        self._require_up()
        # §9.3: the device does not need the console to run a macro, so a run
        # without one is **allowed and warned** rather than refused — refusing
        # would be dishonest about a dependency that does not exist. This is the
        # one place the warning can be given before the fact: once the run is
        # going, nothing about it looks different (G-4).
        if self._status is not None and self._status.console_link is not ConsoleLink.CONNECTED:
            self.log(
                "container",
                "warn",
                f"console: not connected — {entry.source} runs anyway and its inputs go "
                "nowhere until the console connects (§9.3)",
            )
        if self._hello is not None and entry.bytes > self._hello.plan_capacity_bytes:
            # §8.4: validate against HELLO.plan_capacity_bytes before uploading,
            # so PLAN_TOO_LARGE is a container-side pre-check and a device ERROR
            # is the backstop rather than the first line of defence.
            raise ControllerError(
                "PLAN_TOO_LARGE",
                f"{entry.source} compiles to {entry.bytes} bytes, above the board's "
                f"{self._hello.plan_capacity_bytes}-byte capacity.",
            )
        self._cancel_requested = False
        # A new run starts with a fresh stop-reason surface: the story of the
        # previous run's console drop belongs to that run (§9.3).
        self._console_lost = False
        self._uploading = {"macroId": entry.id, "offset": 0, "total": entry.bytes}
        self._publish()
        self.log("container", "info", f"plan: uploading {entry.source} → {entry.bytes} B")

        def progress(offset: int, total: int) -> None:
            if self._cancel_requested:
                raise ControllerError(
                    "UPLOAD_CANCELLED",
                    "The upload was abandoned at the ACK'd offset; the staging buffer is "
                    "discarded and nothing was committed.",
                )
            self._uploading = {"macroId": entry.id, "offset": offset, "total": total}
            self._publish()

        try:
            assert entry.plan is not None
            try:
                await self._device_call(
                    "load_plan",
                    self.device.load_plan(entry.plan.identity, entry.plan.payload, on_progress=progress),
                )
            except PlanHashMismatch as mismatch:
                # §5.6: the board answered, but with an identity the container
                # did not send. Surface it; never start the run.
                self.log("container", "error", f"plan: identity mismatch — {mismatch}")
                raise ControllerError("PLAN_HASH_MISMATCH", str(mismatch)) from None
            self._committed_macro_id = entry.id
            self._committed_bytes = entry.bytes
            self.log("container", "info", f"plan: {entry.source} committed, identity {entry.plan.identity_hex}")
            await self._device_call("start", self.device.start())
            self.log("container", "info", f"mode: MACRO started on {entry.source}")
        finally:
            self._uploading = None
            await self.refresh()

    async def cancel_upload(self) -> None:
        """Abandon a staged transfer: the staging buffer is discarded (§2.7).

        The windowed stream is issue #30's; the skeleton's contract is that the
        abort happens at the ACK'd offset and nothing is committed.
        """
        if self._uploading is None:
            return
        self._cancel_requested = True
        self.log("container", "info", "plan: upload cancellation requested")
        self._publish()

    async def stop_macro(self) -> None:
        self._require_up()
        await self._device_call("stop", self.device.stop())
        self._console_lost = False
        self.log("container", "info", "mode: stopped from the app")
        await self.refresh()

    async def place_figure(self, figure_id: str, *, identity: bytes | None = None) -> None:
        entry = self.figures.resolve(figure_id)
        if entry is None:
            raise ControllerError("UNKNOWN_FIGURE", f"No figure with id {figure_id!r} is indexed.")
        if self._hello is not None and not self._hello.supports(Features.AMIIBO):
            raise ControllerError(
                "AMIIBO_UNSUPPORTED",
                "This firmware reports features.amiibo = false; flash a build with the NFC path.",
            )
        self._refuse_locked_key()
        image = self.figures.read_image(figure_id)
        if image is None:
            raise ControllerError("FIGURE_UNREADABLE", f"{entry.source} is missing or shorter than 540 bytes.")
        self._require_up()
        # §9.3 covers `PLACE_AMIIBO` in the same sentence as `START`: a placement
        # without a console is allowed, and the one consequence worth stating is
        # the same — nothing happens until one connects.
        if self._status is not None and self._status.console_link is not ConsoleLink.CONNECTED:
            self.log(
                "container",
                "warn",
                f"console: not connected — {entry.name} is placed anyway and no scan will "
                "reach it until the console connects (§9.3)",
            )
        sealed = self._seal_figure(image, identity=identity)
        if sealed is None:
            if self._key_status.state is KeyState.KEY_INVALID:
                raise ControllerError(
                    "KEY_INVALID",
                    "The mounted key failed the nfc3d round trip; it is not a usable retail key.",
                )
            raise ControllerError(
                "FIGURE_INVALID",
                f"{entry.source} does not verify under the mounted key — its signatures are "
                "not this figure's, so it cannot be sealed.",
            )
        await self._device_call("place", self.device.place(sealed.image))
        self._placed_figure_id = figure_id
        self._placement_index += 1
        self._placed_at = time.monotonic()
        self._scans = 0
        self._last_polling = 0
        # A fresh placement is virgin — it owes the console no rotation.
        self._rotate_pending = False
        self.log(
            "container",
            "info",
            f"amiibo: {entry.name} placed as {sealed.identity.hex()} (placement #{self._placement_index})",
        )
        await self.refresh()

    async def unplace(self) -> None:
        self._require_up()
        await self._device_call("unplace", self.device.unplace())
        self._placed_figure_id = None
        self._placed_at = None
        self._last_polling = 0
        self._rotate_pending = False
        self.log("container", "info", "amiibo: unplaced")
        await self.refresh()

    async def pair_unpair(self) -> None:
        self._require_up()
        await self._device_call("pair_unpair", self.device.pair_unpair())
        self.log("container", "info", "pairing: the board was asked to forget its bond (the console keeps its own copy)")
        await self.refresh()

    async def save_config(self, *, report_interval_ms: int, led: bool) -> None:
        self._require_up()
        await self._device_call("config", self.device.config(report_interval_ms=report_interval_ms, led=led))
        self._config = ConsoleConfig(report_interval_ms=report_interval_ms, led=led)
        self.log("container", "info", f"config: report_interval_ms={report_interval_ms} led={'on' if led else 'off'}")
        await self.refresh()

    # ── container-local affordances ────────────────────────────────────────

    async def rescan(self) -> None:
        # Libraries only. §6.7: the key is read once at startup and is not
        # hot-reloaded — finding it afterwards means restarting the container.
        self._scan_libraries()
        self.log("container", "info", "library: rescanned on request")
        self._publish()

    async def reconnect(self) -> None:
        await self._attempt_connect()
        self._publish()

    async def _attempt_connect(self) -> bool:
        """One `HELLO`+`STATUS` attempt with the §2.8 `boot_id` branch.

        Returns whether the link is up. A `Port busy` is recorded and never
        retried through (§8.9); the connect loop decides the backoff.
        """
        previous = self._hello
        try:
            await self.device.open()
            hello = await self.device.hello()
        except PortBusy as busy:
            self._held_by = busy.holder or "another process"
            self._control_link = "DOWN"
            self.log("container", "warn", f"control: {busy}")
            return False
        except TransportUnavailable as unavailable:
            self._control_link = "DOWN"
            self._held_by = None
            self.log("container", "warn", f"control: no device on {self.settings.port} — {unavailable}")
            return False
        except CommandError as error:
            self._control_link = "DOWN"
            self.log("container", "warn", f"control: HELLO refused — {error}")
            return False
        except ProtocolError as error:
            self._control_link = "DOWN"
            self.log("container", "error", f"control: the device answered HELLO wrong — {error}")
            return False
        self._hello = hello
        self._held_by = None
        self._control_link = "UP"
        self._recovery = self._recovery_case(previous, hello)
        if self._recovery == "NEW_POWER":
            self._clear_device_truth()
            self.log("container", "warn", "recovery: new power — plan and placement cleared; re-upload required")
        elif self._recovery == "DIFFERENT_FIRMWARE":
            self.log("container", "warn", "recovery: different firmware on the same power — capability-controlled")
        self._on_hello()
        return await self.refresh()

    def _clear_device_truth(self) -> None:
        self._committed_macro_id = None
        self._committed_bytes = 0
        self._placed_figure_id = None
        self._placed_at = None
        self._rotate_pending = False
        # §9.2: a reboot during a macro silently ends the run and the reason is
        # not knowable from the device — `NONE`, never an invented one. The
        # story of a pre-reboot console drop does not survive the reset either.
        self._console_lost = False

    async def _device_call(self, label: str, awaitable):
        """Await one device call, turning a dead link into a typed refusal.

        The link state is marked down here rather than at the next poll, so the
        header is honest the moment a verb fails. The verb itself is never
        retried (§8.9, §8.10).
        """
        try:
            return await awaitable
        except PortBusy as busy:
            self._held_by = busy.holder or "another process"
            self._control_link = "DOWN"
            self._publish()
            raise ControllerError("PORT_BUSY", str(busy)) from None
        except TransportUnavailable as unavailable:
            self._control_link = "DOWN"
            self._held_by = None
            self._publish()
            raise ControllerError("NO_DEVICE", f"{label}: {unavailable}") from None
        except ProtocolError as error:
            self.log("container", "error", f"control: {label} read a malformed frame — {error}")
            self._publish()
            raise ControllerError("PROTOCOL_ERROR", str(error)) from None

    @staticmethod
    def _recovery_case(previous: Hello | None, current: Hello) -> str:
        if previous is None:
            return "SAME_POWER"
        if current.boot_id != previous.boot_id:
            return "NEW_POWER"
        if current.fw_version != previous.fw_version:
            return "DIFFERENT_FIRMWARE"
        return "SAME_POWER"

    async def refresh(self) -> bool:
        try:
            self._status = await self.device.status()
            self._on_status()
        except (TransportUnavailable, CommandError, ProtocolError) as failure:
            self._control_link = "DOWN"
            self.log("container", "warn", f"control: status failed — {failure}")
            self._publish()
            return False
        self._publish()
        return True

    def clear_logs(self) -> None:
        self._logs.clear()
        self._publish()

    def log(self, source: str, level: str, message: str) -> None:
        self._log_seq += 1
        line = LogLine(
            id=self._log_seq,
            t=time.monotonic() - self._started_at,
            source=source,
            level=level,
            message=message,
        )
        self._logs.append(line)
        # Also to the process log, so `docker logs` tells the same story the
        # Logs screen does (§6.7's startup line included).
        logger.log(_LOG_LEVELS.get(level, logging.INFO), "[%s] %s", source, message)
        self._dispatch("log", line.to_dict())

    # ── internals ──────────────────────────────────────────────────────────

    def _require_up(self) -> None:
        if self._control_link != "UP":
            raise ControllerError("NO_DEVICE", "No board answered on the control link.")

    def _refuse_locked_key(self) -> None:
        state = self._key_status.state
        if state is KeyState.KEY_ABSENT:
            raise ControllerError(
                "KEY_ABSENT",
                "No key material is mounted. Mount your own key_retail.bin read-only at "
                "/keys/key_retail.bin and restart the container.",
            )
        if state is KeyState.KEY_INVALID:
            raise ControllerError(
                "KEY_INVALID",
                "The mounted key failed validation. Use the single 160 B key_retail.bin, or "
                "unfixed-info.bin + locked-secret.bin concatenated in that order.",
            )

    def _seal_figure(self, image: bytes, *, identity: bytes | None = None) -> SealedTag | None:
        """One sealing attempt — the shared core of placing and rotating.

        Returns `None` when sealing cannot happen, having already set
        `KEY_INVALID` if that is what the failure proved (§6.7: a failure at a
        placement sets the state; it never falls back to replaying the stored
        tag unchanged). With no library tag at startup the key is
        `KEY_UNVERIFIED`, and the first placement is exactly where verification
        completes — a failure there is the key's. Once the key is `KEY_OK` the
        same failure is the figure's (a corrupt tag image), and the key keeps its
        verdict.
        """
        material = self._key_status.material
        if material is None:
            return None
        try:
            sealed = self._sealer(image, material, identity=identity)
        except (KeyInvalid, TagInvalid) as failure:
            if self._key_status.state is KeyState.KEY_UNVERIFIED:
                self._key_status = KeyStatus(
                    KeyState.KEY_INVALID,
                    self._key_status.spelling,
                    None,
                    detail=str(failure),
                    length=self._key_status.length,
                    fingerprint=self._key_status.fingerprint,
                )
                self.log(
                    "container",
                    "error",
                    f"key: the first placement failed verification — {failure}",
                )
            return None
        if self._key_status.state is KeyState.KEY_UNVERIFIED:
            # §6.7: verification completes at the first placement, so the state
            # moves with it. Without this the badge keeps calling a proven key
            # unproven, and the *next* bad figure would be blamed on the key.
            self._key_status = KeyStatus(
                KeyState.KEY_OK,
                self._key_status.spelling,
                material,
                length=self._key_status.length,
                fingerprint=self._key_status.fingerprint,
            )
            self.log("container", "info", "key: the first placement verified the key material")
        return sealed

    def _scan_libraries(self) -> None:
        macros = self.macros.scan()
        figures = self.figures.scan()
        ready = sum(1 for entry in macros if entry.runnable)
        rejected = len(macros) - ready
        self.log("container", "info", f"macro library: {len(macros)} files scanned, {ready} compiled, {rejected} rejected")
        for entry in macros:
            if entry.rejection is not None:
                self.log("container", "warn", f"macro rejected: {entry.source}: {entry.rejection.message}")
        self.log("container", "info", f"amiibo library: {len(figures)} .bin indexed across {len(self.figures.series)} series")

    def _key_line(self) -> str:
        """§6.7's one startup line: path, spelling, length, fingerprint, state.

        Never the bytes: the truncated SHA-256 is what answers "which key is
        loaded" without answering "what is the key".
        """
        status = self._key_status
        if status.state is KeyState.KEY_ABSENT:
            return f"key: {self.keys.key_file} absent — Amiibo is offered but locked"
        line = f"key: {self.keys.key_file} ({status.spelling}, {status.length} B)"
        if status.fingerprint is not None:
            line += f" fp=sha256:{status.fingerprint}…"
        line += f" state={status.state.value}"
        if status.detail is not None:
            line += f" — {status.detail}"
        return line

    def _on_hello(self) -> None:
        assert self._hello is not None
        hello = self._hello
        self.log(
            "container",
            "info",
            f"hello: proto_ver={hello.proto_ver} fw={hello.fw_version} boot_id={hello.boot_id:08x} "
            f"features={'|'.join(self._feature_names(hello)) or 'none'}",
        )

    @staticmethod
    def _feature_names(hello: Hello) -> list[str]:
        names = []
        if hello.supports(Features.MACRO):
            names.append("macro")
        if hello.supports(Features.AMIIBO):
            names.append("amiibo")
        if hello.supports(Features.CONFIG):
            names.append("config")
        return names

    def _on_status(self) -> None:
        status = self._status
        assert status is not None
        # Console scan counting is the container's: the wire carries
        # `console_polling`, not a scan count (§3.2). The *story* of a scan
        # ending comes from the SCAN_ENDED event (§3.3); the level here only
        # counts, so the Logs screen does not say it twice.
        polling = int(status.console_polling)
        if status.tag_state is TagState.PLACED and polling > 0 and self._last_polling == 0:
            self._scans += 1
        self._last_polling = polling

    def _stop_reason(self, status: Status) -> str:
        """The UI's reason surface, derived from wire facts — not a wire value.

        §3.2's `last_stop_reason` is a closed set of *who stopped it*
        (`NONE`/`CONTAINER_STOP`/`BOOT_LOCAL`); `ERROR` and `CONSOLE_LOST` are
        the container-side readings §3.4's table and chapter 9's policy define,
        and `ui-contract.md` fixes them as the UI's four stop reasons.
        """
        if status.last_stop_reason is StopReason.BOOT_LOCAL:
            return "BOOT_LOCAL"
        if status.last_error_code is not ErrorCode.NONE and status.last_stop_reason is StopReason.NONE:
            return "ERROR"
        if self._console_lost:
            return "CONSOLE_LOST"
        if status.last_stop_reason is StopReason.CONTAINER_STOP:
            return "CONTAINER_STOP"
        return "NONE"

    async def _poll_loop(self) -> None:
        period = 1.0 / max(0.1, self.settings.status_hz)
        while not self._closing:
            await asyncio.sleep(period)
            if self._control_link != "UP":
                # Reconnecting is the connect loop's job; polling a dead port would
                # turn a backoff into a 2 Hz reopen storm (§8.9, §8.10).
                continue
            try:
                self._status = await self.device.status()
                self._control_link = "UP"
                self._on_status()
            except (TransportUnavailable, CommandError, ProtocolError) as failure:
                if self._control_link == "UP":
                    self.log("container", "warn", f"control: status failed — {failure}")
                self._control_link = "DOWN"
            except asyncio.CancelledError:
                raise
            except Exception as failure:  # pragma: no cover - defensive
                self.log("container", "error", f"control: unexpected status failure — {failure}")
                self._control_link = "DOWN"
            self._publish()

    async def _connect_loop(self) -> None:
        """Retry the open with backoff while the link is down (§8.9).

        Never retries through a held port: a `Port busy` is a deployment error
        the user fixes, and `Reconnect` is the manual retry.
        """
        backoff = 1.0
        while not self._closing:
            await asyncio.sleep(backoff)
            if self._control_link == "UP":
                backoff = 1.0
                continue
            if self._held_by is not None:
                backoff = min(backoff * 2.0, 30.0)
                continue
            if await self._attempt_connect():
                backoff = 1.0
                self.log("container", "info", "control: reconnected without a manual step")
            else:
                backoff = min(backoff * 2.0, 30.0)
            self._publish()

    async def _log_loop(self) -> None:
        """Forward the device's `ESP_LOG` lines to the Logs screen (§8.2, §8.7)."""
        try:
            async for level, message in self.device.log_lines():
                self.log("device", level, message)
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # pragma: no cover - defensive
            self.log("container", "warn", f"device log stream stopped: {failure}")

    async def _event_loop(self) -> None:
        prompts = {
            EventKind.MODE_CHANGED,
            EventKind.PLAN_COMMITTED,
            EventKind.PLAN_DISCARDED,
            EventKind.TAG_PLACED,
            EventKind.TAG_UNPLACED,
        }
        try:
            async for event in self.device.events():
                self._on_event(event)
                # §3.1: an event is a prompt to re-read STATUS, never a substitute.
                if event.kind is EventKind.BOOT:
                    # §2.8/§8.3: a mid-session BOOT means the same as a reconnect —
                    # HELLO, then the boot_id branch decides what survives.
                    await self._attempt_connect()
                    self._publish()
                elif event.kind is EventKind.CONSOLE_LINK:
                    # §3.5: chapter 9's policy, applied here and nowhere else.
                    await self._on_console_link(event)
                elif event.kind is EventKind.SCAN_ENDED:
                    await self._on_scan_ended()
                elif event.kind in prompts:
                    await self.refresh()
                else:
                    self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # pragma: no cover - defensive
            self.log("container", "error", f"event stream stopped: {failure}")

    def _on_event(self, event: Event) -> None:
        if event.kind is EventKind.BOOT:
            boot_id = event.boot_id
            # §2.8: BOOT is a hint that the link needs a fresh `HELLO`; whether
            # the plan and placement survive is the boot_id branch's call, taken
            # in `_attempt_connect`, not this handler's.
            self.log(
                "container",
                "warn",
                f"recovery: the device reported a boot (boot_id={boot_id:08x})"
                if boot_id is not None
                else "recovery: the device reported a boot",
            )
        elif event.kind is EventKind.MODE_CHANGED:
            mode = event.mode
            self.log("frame", "debug", f"EVT  MODE_CHANGED   mode={mode.name if mode else '?'}")
        elif event.kind is EventKind.PLAN_COMMITTED:
            self.log("frame", "debug", "EVT  PLAN_COMMITTED")
        elif event.kind is EventKind.PLAN_DISCARDED:
            self._committed_macro_id = None
            self._committed_bytes = 0
            self._placed_figure_id = None
            self._rotate_pending = False
            self.log("container", "warn", "plan: discarded by a long press at the board")
        elif event.kind is EventKind.TAG_PLACED:
            self.log("frame", "debug", "EVT  TAG_PLACED")
        elif event.kind is EventKind.TAG_UNPLACED:
            self.log("frame", "debug", "EVT  TAG_UNPLACED")
        elif event.kind is EventKind.ERROR_RAISED:
            error = event.error
            if error is not None:
                self.log("device", "error", f"the board raised {error.code.name}")

    # ── chapter 9: the console-lifecycle policy (§9.3, §8.3) ───────────

    async def _on_console_link(self, event: Event) -> None:
        """Apply chapter 9's policy to one `CONSOLE_LINK` edge, from STATUS alone.

        The device watches neither link (ADR-0008), so every decision here is
        the container's, and the device never learns *why* it was stopped:
        `CONSOLE_LOST` is a container-side reading, never a wire value.
        """
        edge = event.link_edge
        if edge is None:
            self.log("frame", "debug", "EVT  CONSOLE_LINK     <short payload>")
            self._publish()
            return
        which, reason = edge
        if which is LinkEdge.DISCONNECTED:
            self._last_drop_reason = self._describe_drop(reason)
            self.log("container", "warn", f"console link: disconnected (reason={self._last_drop_reason})")
            # §3.1: the action is decided from re-read truth, not from the event.
            await self.refresh()
            status = self._status
            if status is not None and status.mode is Mode.MACRO:
                # §9.3: a pass resuming mid-press into a freshly-connected
                # console is an unverified risk (G-5) not worth one verb.
                # `STOP` is idempotent in IDLE (§4.4), so a duplicate edge or a
                # run that already ended sends nothing further.
                try:
                    await self._device_call("stop", self.device.stop())
                except (ControllerError, CommandError) as failure:
                    self.log(
                        "container",
                        "error",
                        f"console: the stop did not reach the device — {failure}; "
                        "the run keeps replaying until it does",
                    )
                    self._publish()
                    return
                self._console_lost = True
                self.log(
                    "container",
                    "warn",
                    "console: macro stopped — the console re-initialises itself on "
                    "reconnect, so restarting is yours to do",
                )
                # Read the mode back rather than publishing the pre-stop one:
                # no optimistic mode change, in either direction (§8.2).
                await self.refresh()
            elif status is not None and status.tag_state is TagState.PLACED:
                # §9.3/§6.5: the placement is kept — a tag nobody is reading is
                # harmless — and rotated before the console's next scan.
                self._rotate_pending = True
                self.log(
                    "container",
                    "info",
                    "amiibo: placement kept across the drop; it will be rotated "
                    "before the console's next scan",
                )
            self._publish()
            return
        # An up-edge: CONNECTED, or the RESUBSCRIBED the console issues itself
        # on wake (§9.1). The console re-runs its whole init and needs nothing
        # re-sent, so the only policy here is the rotation.
        self.log("container", "info", f"console link: {which.name.lower()}")
        await self.refresh()
        if self._rotate_pending:
            await self._rotate_placement(trigger="the console reconnected")
        self._publish()

    async def _on_scan_ended(self) -> None:
        """§6.5/§3.5: one placement per scan — mint the next identity now.

        The same action the reconnect policy takes (§9.3), not new machinery:
        the rotation is one atomic `PLACE_AMIIBO` whose tag-absent gap the device
        emits itself.
        """
        self.log("container", "info", "amiibo: the console finished a scan")
        # §3.1: the event is the prompt; the rotation is decided from re-read
        # truth (the tag may already be gone by the time this runs).
        await self.refresh()
        await self._rotate_placement(trigger="the scan ended")

    async def _rotate_placement(self, *, trigger: str) -> None:
        """Re-seal the placed figure under a fresh identity and re-place it.

        A failure is a warning, never a crash: the event loop must survive it,
        and the placement stays as it is (unplacing would exit the mode for no
        reason, §6.5). A failed rotation is re-armed — deliberately, and not as
        a slow-reply retransmit (§8.9 bans those): the freshness requirement is
        still unmet, and the next up-edge or scan is a new policy trigger, the
        same edge-driven refresh §6.5 defines.
        """
        status = self._status
        figure_id = self._placed_figure_id
        if (
            figure_id is None
            or status is None
            or status.tag_state is not TagState.PLACED
            or self._control_link != "UP"
        ):
            # Nothing placed to rotate — a panic stop, a reboot or a manual
            # unplace got there first, and restarting any of those is §8.10's
            # line, not this policy's.
            self._rotate_pending = False
            return
        image = self.figures.read_image(figure_id)
        if image is None:
            self._rotate_pending = False
            self.log(
                "container",
                "warn",
                f"amiibo: could not rotate after {trigger} — {figure_id} is no longer readable",
            )
            return
        sealed = self._seal_figure(image)
        if sealed is None:
            self.log(
                "container",
                "warn",
                f"amiibo: could not rotate after {trigger} — sealing failed; the console "
                "may re-scan the identity it already saw",
            )
            return
        try:
            await self._device_call("place", self.device.place(sealed.image))
        except (ControllerError, CommandError) as failure:
            self.log(
                "container",
                "warn",
                f"amiibo: the rotation did not reach the device — {failure}",
            )
            return
        self._rotate_pending = False
        self._placement_index += 1
        self._placed_at = time.monotonic()
        self._scans = 0
        self.log(
            "container",
            "info",
            f"amiibo: rotated to {sealed.identity.hex()} (placement #{self._placement_index}) "
            f"after {trigger} — the console will not re-scan an identity it saw",
        )
        await self.refresh()

    @staticmethod
    def _describe_drop(reason: int) -> str:
        if reason == 531:
            return "531 (BLE_HS_ERR_HCI_BASE + 0x13)"
        return str(reason)

    def _dispatch(self, name: str, payload: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait((name, payload))

    def _publish(self) -> None:
        self._dispatch("state", self.snapshot())

    @staticmethod
    def _macro_json(entry: MacroEntry) -> dict:
        rejection = None
        if entry.rejection is not None:
            rejection = {
                "code": entry.rejection.code,
                "message": entry.rejection.message,
                "fix": entry.rejection.fix,
                "atMs": entry.rejection.at_ms,
            }
        return {
            "id": entry.id,
            "name": entry.name,
            "source": entry.source,
            "status": entry.status,
            "events": entry.events,
            "loopMs": entry.loop_ms,
            "buttons": entry.buttons,
            "sticks": entry.sticks,
            "bytes": entry.bytes,
            "rejection": rejection,
        }


__all__ = ["Controller", "ControllerError", "ERROR_SENTENCES", "LogLine", "SealingSeam", "describe_error"]
