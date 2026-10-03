"""The container's two read-only library mounts and the key mount (§8.4–§8.6, §6.7).

The macro library is real: it walks `/library/macros`, compiles every `.json`
through `ns2plan` with all-or-nothing ingestion, and **keeps a rejected macro
in the list with its reason** — hiding it is how a user concludes the file is
missing. The cache is `ns2plan.PlanCache`, keyed by file identity and in
memory only.

The amiibo index here is deliberately structural: it enforces §6.2's intake
rules that the screens depend on (slice the first 540 B, key by file, exclude
`!Essential Files/`) but it does **not** seal, unpack or validate — that is
issue #32's, and this module says so rather than half-doing it. The key store
runs only the first rung of §6.7's ladder (presence and length); a present key
is `KEY_UNVERIFIED` until #32 can run the unpack round trip.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..ns2plan import MacroRejected, Plan, PlanCache
from ..ns2sealing import KEY_LENGTH, TAG_IMAGE_LENGTH, KeyInvalid, KeyMaterial

#: §8.5 — the key is not a figure.
ESSENTIAL_DIR = "!Essential Files"


class KeyState(str, Enum):
    """§6.7's four states, container-side only (the wire has none of them)."""

    KEY_READY = "KEY_READY"
    KEY_ABSENT = "KEY_ABSENT"
    KEY_INVALID = "KEY_INVALID"
    KEY_UNVERIFIED = "KEY_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class Rejection:
    code: str
    message: str
    fix: str
    at_ms: float | None = None


@dataclass(frozen=True, slots=True)
class MacroEntry:
    """One macro file as the Control screen sees it (§8.4)."""

    id: str
    name: str
    source: str
    status: str  # "ready" | "rejected"
    events: int = 0
    loop_ms: int = 0
    buttons: int = 0
    sticks: int = 0
    bytes: int = 0
    plan: Plan | None = None
    rejection: Rejection | None = None

    @property
    def runnable(self) -> bool:
        return self.status == "ready" and self.plan is not None


def _macro_stats(raw: bytes) -> tuple[int, int, int]:
    """`(events, buttons, sticks)` from the source document, best effort only."""
    try:
        document = json.loads(raw)
    except ValueError:
        return 0, 0, 0
    if not isinstance(document, list):
        return 0, 0, 0
    buttons = sticks = 0
    for event in document:
        kind = event.get("ev", {}).get("type") if isinstance(event, dict) else None
        if kind == "button":
            buttons += 1
        elif kind == "stick":
            sticks += 1
    return len(document), buttons, sticks


class MacroLibrary:
    """`/library/macros`, scanned at startup and by the one `Rescan` affordance."""

    def __init__(self, root: Path, *, capacity_bytes: int) -> None:
        self.root = Path(root)
        self.capacity_bytes = capacity_bytes
        self.cache = PlanCache()
        self.entries: list[MacroEntry] = []
        self.mounted = False

    def scan(self) -> list[MacroEntry]:
        entries: list[MacroEntry] = []
        if not self.root.is_dir():
            self.entries = []
            self.mounted = False
            return self.entries
        self.mounted = True
        for path in sorted(self.root.rglob("*.json")):
            if not path.is_file():
                continue
            entries.append(self._load(path))
        self.entries = entries
        return entries

    def resolve(self, macro_id: str) -> MacroEntry | None:
        return next((entry for entry in self.entries if entry.id == macro_id), None)

    def _load(self, path: Path) -> MacroEntry:
        relative = os.path.relpath(path, self.root)
        raw = path.read_bytes()
        events, buttons, sticks = _macro_stats(raw)
        try:
            plan = self.cache.load(path, capacity_bytes=self.capacity_bytes)
        except MacroRejected as rejected:
            return MacroEntry(
                id=relative,
                name=path.stem,
                source=relative,
                status="rejected",
                events=events,
                buttons=buttons,
                sticks=sticks,
                rejection=Rejection(
                    code=rejected.code,
                    message=rejected.reason,
                    fix=(
                        "Only `button` and `stick` events are accepted; fix the event "
                        "and rescan the library."
                    ),
                    at_ms=rejected.t_ms,
                ),
            )
        return MacroEntry(
            id=relative,
            name=path.stem,
            source=relative,
            status="ready",
            events=events,
            loop_ms=plan.loop_ms,
            buttons=buttons,
            sticks=sticks,
            bytes=plan.size,
            plan=plan,
        )

    @property
    def empty(self) -> bool:
        return not self.entries


@dataclass(frozen=True, slots=True)
class FigureEntry:
    id: str
    name: str
    series: str
    source: str


class AmiiboIndex:
    """`/library/amiibo`, indexed structurally. Sealing and validation are #32's."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries: list[FigureEntry] = []
        self.mounted = False

    def scan(self) -> list[FigureEntry]:
        entries: list[FigureEntry] = []
        if not self.root.is_dir():
            self.entries = []
            self.mounted = False
            return self.entries
        self.mounted = True
        for path in sorted(self.root.rglob("*.bin")):
            if not path.is_file() or ESSENTIAL_DIR in path.parts:
                continue
            relative = os.path.relpath(path, self.root)
            entries.append(
                FigureEntry(
                    id=relative,
                    name=path.stem,
                    series=path.parent.name,
                    source=relative,
                )
            )
        self.entries = entries
        return entries

    def resolve(self, figure_id: str) -> FigureEntry | None:
        return next((entry for entry in self.entries if entry.id == figure_id), None)

    def read_image(self, figure_id: str) -> bytes | None:
        """The first 540 bytes of a figure's dump, or `None` if it is gone."""
        entry = self.resolve(figure_id)
        if entry is None:
            return None
        data = (self.root / entry.source).read_bytes()
        if len(data) < TAG_IMAGE_LENGTH:
            return None
        return data[:TAG_IMAGE_LENGTH]

    @property
    def series(self) -> list[str]:
        return sorted({entry.series for entry in self.entries})

    @property
    def empty(self) -> bool:
        return not self.entries


@dataclass(frozen=True, slots=True)
class KeyStatus:
    state: KeyState
    spelling: str | None = None
    material: KeyMaterial | None = None

    @property
    def label(self) -> str:
        return self.state.value


class KeyStore:
    """§6.7's fixed path, read once: rungs 1–2 of the ladder, not rung 3.

    Rung 3 is an `unpack` round trip against a library tag, which needs the
    sealer from issue #32. Until then a present, correctly-sized key is
    `KEY_UNVERIFIED` — never a false `KEY_READY`.
    """

    def __init__(self, key_file: Path, key_dir: Path | None = None) -> None:
        self.key_file = Path(key_file)
        self.key_dir = Path(key_dir) if key_dir is not None else self.key_file.parent

    def read(self) -> KeyStatus:
        if self.key_file.is_file():
            return self._from_bytes(self.key_file.read_bytes(), "single file (key_retail.bin)")
        two_file = self._read_two_file()
        if two_file is not None:
            return two_file
        return KeyStatus(KeyState.KEY_ABSENT)

    def _read_two_file(self) -> KeyStatus | None:
        if not self.key_dir.is_dir():
            return None
        unfixed = self.key_dir / "unfixed-info.bin"
        locked = self.key_dir / "locked-secret.bin"
        if not (unfixed.is_file() and locked.is_file()):
            return None
        return self._from_bytes(
            unfixed.read_bytes() + locked.read_bytes(),
            "two files (unfixed-info.bin + locked-secret.bin)",
        )

    def _from_bytes(self, data: bytes, spelling: str) -> KeyStatus:
        try:
            material = KeyMaterial(data=data, spelling=spelling)
        except KeyInvalid:
            return KeyStatus(KeyState.KEY_INVALID, spelling, None)
        # Rung 3 (the unpack round trip) is issue #32's; do not claim KEY_READY.
        return KeyStatus(KeyState.KEY_UNVERIFIED, spelling, material)


__all__ = [
    "AmiiboIndex",
    "ESSENTIAL_DIR",
    "FigureEntry",
    "KEY_LENGTH",
    "KeyState",
    "KeyStatus",
    "KeyStore",
    "MacroEntry",
    "MacroLibrary",
    "Rejection",
]
