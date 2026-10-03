"""The container's two read-only library mounts and the key mount (§8.4–§8.6, §6.7).

The macro library is real: it walks `/library/macros`, compiles every `.json`
through `ns2plan` with all-or-nothing ingestion, and **keeps a rejected macro
in the list with its reason** — hiding it is how a user concludes the file is
missing. The cache is `ns2plan.PlanCache`, keyed by file identity and in
memory only.

The amiibo index is §8.5's: `.bin` canonical and sliced to the first 540 B,
`.nfc` ignored, `!Essential Files/` excluded, keyed by file and grouped by
figure and series — never by ID, which is not unique (846 IDs across 955
files). `KeyStore` runs §6.7's whole ladder, whose third rung is an `unpack`
round trip against the mounted library.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..ns2plan import MacroRejected, Plan, PlanCache
from ..ns2sealing import (
    KEY_LENGTH,
    TAG_IMAGE_LENGTH,
    KeyInvalid,
    KeyMaterial,
    TagInvalid,
    unpack,
)

#: §8.5 — the key is not a figure.
ESSENTIAL_DIR = "!Essential Files"

#: How many library figures rung 3 may try before it calls the key invalid.
#: One would be §6.7's literal reading, but the corpus ships a genuinely bad
#: tag image (`Pikmin Amiibo/Pikmin.bin`; amiitool refuses it too), so a single
#: sample would falsely accuse a good key once in 951 figures.
VERIFICATION_SAMPLE = 8


class KeyState(str, Enum):
    """§6.7's four states, container-side only (the wire has none of them)."""

    KEY_OK = "KEY_OK"
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
    """`/library/amiibo`, indexed by file and grouped by figure and series (§8.5).

    **Never grouped or keyed by amiibo ID**: 846 distinct IDs live across 955
    files, four of them carrying multiple UIDs, so an ID-keyed index would drop
    figures. The file's relative path is the identity, and the slice (not the
    file size) is the intake rule.
    """

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
        """The first 540 bytes of a figure's tag image, or `None` if it is gone."""
        entry = self.resolve(figure_id)
        if entry is None:
            return None
        data = (self.root / entry.source).read_bytes()
        if len(data) < TAG_IMAGE_LENGTH:
            return None
        return data[:TAG_IMAGE_LENGTH]

    def sample_images(self, limit: int) -> list[bytes]:
        """The first `limit` readable figures, for §6.7's rung 3.

        Deterministic (the index is sorted by path) so the key's verification is
        reproducible across restarts, and bounded so a bad key costs a few
        milliseconds rather than 951 figures' worth.
        """
        images: list[bytes] = []
        for entry in self.entries:
            if len(images) >= limit:
                break
            image = self.read_image(entry.id)
            if image is not None:
                images.append(image)
        return images

    @property
    def series(self) -> list[str]:
        return sorted({entry.series for entry in self.entries})

    @property
    def empty(self) -> bool:
        return not self.entries


def fingerprint_of(data: bytes) -> str:
    """The truncated SHA-256 of the bytes that were read (§6.7's startup line).

    Truncated because it answers "which key is loaded" without answering "what is
    the key"; it is computed for a *rejected* file too, because two different
    wrong keys are otherwise indistinguishable from each other in a log.
    """
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class KeyStatus:
    state: KeyState
    spelling: str | None = None
    material: KeyMaterial | None = None
    #: Which rung failed, for the startup log line. Never key bytes.
    detail: str | None = None
    #: How many bytes were read, even when they were rejected.
    length: int | None = None
    #: `fingerprint_of` the bytes read, even when they were rejected.
    fingerprint: str | None = None

    @property
    def label(self) -> str:
        return self.state.value


class KeyStore:
    """§6.7's fixed path, read once, through the whole three-rung ladder.

    ```text
    1. length (160 B, or the two-file spelling)
    2. the masterkey structure — Nintendo's typeStrings and magicBytes
    3. an unpack round trip against the mounted library, both HMACs
    ```

    Rung 3 needs a figure, so the store is given the index and asks it for a
    bounded sample. A library that is mounted but unreadable leaves the key
    `KEY_UNVERIFIED` rather than inventing a verdict; the first placement then
    completes the verification (§6.7).
    """

    def __init__(
        self,
        key_file: Path,
        key_dir: Path | None = None,
        *,
        index: AmiiboIndex | None = None,
        sample: int = VERIFICATION_SAMPLE,
    ) -> None:
        self.key_file = Path(key_file)
        self.key_dir = Path(key_dir) if key_dir is not None else self.key_file.parent
        self.index = index
        self.sample = sample

    def read(self) -> KeyStatus:
        """Rungs 1–3, in order, over the one file that is read (§6.7)."""
        if self.key_file.is_file():
            return self._from_bytes(self.key_file.read_bytes(), "single file")
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
        # The fingerprint is taken before the verdict, so a rejected file is
        # still identifiable in the startup line (§6.7).
        facts: dict = {"spelling": spelling, "length": len(data), "fingerprint": fingerprint_of(data)}
        try:
            material = KeyMaterial(data=data, spelling=spelling)
        except KeyInvalid as rejected:
            return KeyStatus(KeyState.KEY_INVALID, detail=str(rejected), **facts)
        return self._verify(material, **facts)

    def _verify(
        self, material: KeyMaterial, *, spelling: str, length: int, fingerprint: str
    ) -> KeyStatus:
        """Rung 3: an `unpack` round trip, which requires both HMACs.

        A failure is the key's, not the figure's: a *structurally valid* key that
        cannot verify a tag from the mounted library is the wrong key, and the
        one action that fixes it is the one §6.7's table names. With no library
        to try, the honest answer is `KEY_UNVERIFIED`, not `KEY_OK` (a lie) and
        not `KEY_INVALID` (a false accusation).
        """
        samples = self.index.sample_images(self.sample) if self.index is not None else []
        if not samples:
            return KeyStatus(
                KeyState.KEY_UNVERIFIED, spelling, material, length=length, fingerprint=fingerprint
            )
        failures = 0
        for image in samples:
            try:
                unpack(image, material)
            except TagInvalid:
                failures += 1
                continue
            return KeyStatus(KeyState.KEY_OK, spelling, material, length=length, fingerprint=fingerprint)
        return KeyStatus(
            KeyState.KEY_INVALID,
            spelling,
            None,
            detail=f"no figure of {failures} verified",
            length=length,
            fingerprint=fingerprint,
        )


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
    "VERIFICATION_SAMPLE",
]
