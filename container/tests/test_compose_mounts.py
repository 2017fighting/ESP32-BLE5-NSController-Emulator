#!/usr/bin/env python3
"""The corpus root is spelled once, as `${REFERENCE_ROOT:-$HOME/clone}` (#43).

Issue #43: the deployment line mounted the retail key from
`$HOME/clone/Amiibo/!Essential Files/key_retail.bin`, but this corpus keeps it *inside* the
`Amiibo Bin/` tree (`machine-prep.md` §6 — the authority on the layout), which is the spelling
`support.reference_key_file()` had resolved all along. Docker does not fail on a missing bind
source: it creates a **directory**. So the wrong path did not read as a wrong path — the
container reported `KEY_ABSENT` at startup, blaming the operator for a key they had mounted,
and the run wrote an empty `!Essential Files/key_retail.bin` directory into the pinned clone of
`docs/references.md`, which stops the clone matching its pin.

Three surfaces print the same three mounts, and fixing one while leaving the others behind
re-issues the defect on the surface a reader copies from:

| Surface | Why it carries the paths |
| --- | --- |
| `container/compose.yaml` | the executable file; §10.4's YAML block is its `services:` bytes (§10.4, issue #42) |
| `docs/spec/10-deployment.md` §10.4 | the deployment line and the two `docker run` equivalents |
| `container/web/src/lib/mounts.ts` | the UI's mount lines, which ADR-0012 requires to be printable verbatim |

The guard is hermetic — text only, no Docker, no corpus, no YAML parser — and derives every
expected path from `support.py`, so the file and the tests cannot drift apart again.

Compose interpolates `${REFERENCE_ROOT:-$HOME/clone}` at parse time and does **not** check the
path (`:-` substitutes rather than fails), so a wrong-but-set value still becomes a
Docker-created directory; §10.4 states that and gives the preflight. The research record the
block came from (`container-and-web-ui.md` §4) keeps the old spelling and is left as written —
a research record is the evidence trail, not the specification (§00, Citations).

Run:  python3 -m unittest discover -s container/tests -p 'test_*.py'
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

COMPOSE = support.REPO_ROOT / "container" / "compose.yaml"
DEPLOYMENT = support.REPO_ROOT / "docs" / "spec" / "10-deployment.md"
UI_MOUNTS = support.REPO_ROOT / "container" / "web" / "src" / "lib" / "mounts.ts"

#: The section that owns the deployment line.
SECTION = "## 10.4 Run the container"
#: The next section, so a scan of §10.4 cannot wander into §10.5.
SECTION_END = "## 10.5 Operational rules"

#: One substitution, so a host whose corpus is not under `~/clone` changes no file: the
#: same variable and default `docs/references.md` resolves (`$REFERENCE_ROOT/<alias>`).
ROOT_SUBSTITUTION = "${REFERENCE_ROOT:-$HOME/clone}"
SUBSTITUTION = re.compile(r"\$\{REFERENCE_ROOT:-(?P<default>\$HOME/clone)\}")

#: The three container-side targets §10.4 fixes, each against the corpus path it reads.
MOUNT_TARGETS = {
    "/library/macros": support.REFERENCE_MACRO_RELATIVE,
    "/library/amiibo": support.REFERENCE_AMIIBO_RELATIVE,
    "/keys/key_retail.bin": support.REFERENCE_KEY_RELATIVE,
}

#: The UI's four lines, each against the corpus path it names (`keyDir` is the pair spelling).
UI_MOUNT_LINES = {
    "keyFile": support.REFERENCE_KEY_RELATIVE,
    "keyDir": support.REFERENCE_KEY_RELATIVE.parent,
    "macros": support.REFERENCE_MACRO_RELATIVE,
    "amiibo": support.REFERENCE_AMIIBO_RELATIVE,
}

#: `-v "<host source>":<target>:ro` and `- "<host source>:<target>:ro"` — the docker-run
#: and the YAML shapes differ only in which side of the source the quotes close on, so the
#: source runs back to the opening quote (never across one) and the closing quote is optional.
MOUNT_LINE = re.compile(
    r"(?P<source>[^\"\n]*?)\"?:(?P<target>/library/macros|/library/amiibo|/keys/key_retail\.bin):ro"
)
UI_LINE = re.compile(r"^ {2}(?P<key>\w+): `-v \"(?P<source>[^\"]+)\":", re.MULTILINE)
#: A TS template literal escapes the dollar of a shell `${…}`; the UI prints what it evaluates
#: to, so the guard drops that one backslash and compares the evaluated line.
TS_ESCAPE = re.compile(r"\\(?=\$\{)")


def expected_source(relative: Path) -> str:
    """The one legal spelling of a corpus root: the substitution, then the relative path."""
    return f"{ROOT_SUBSTITUTION}/{relative.as_posix()}"


def resolved(source: str) -> Path:
    """`${REFERENCE_ROOT:-$HOME/clone}` resolved the way Compose resolves it."""

    def substitute(match: re.Match[str]) -> str:
        return os.environ.get("REFERENCE_ROOT") or os.path.expanduser(
            match["default"].replace("$HOME", "~")
        )

    return Path(SUBSTITUTION.sub(substitute, source))


def mount_lines(text: str) -> list[tuple[str, str]]:
    """Every mount line in `text`, as `(host source, container target)`."""
    return [(match["source"], match["target"]) for match in MOUNT_LINE.finditer(text)]


def deployment_section() -> str:
    text = DEPLOYMENT.read_text(encoding="utf-8")
    section = text[text.index(SECTION) :]
    return section[: section.index(SECTION_END)]


def ui_mount_lines() -> dict[str, str]:
    """`mounts.ts`'s `MOUNTS` values by key, as the host source each prints."""
    lines = {}
    for match in UI_LINE.finditer(UI_MOUNTS.read_text(encoding="utf-8")):
        lines[match["key"]] = TS_ESCAPE.sub("", match["source"])
    return lines


class TheCorpusRootIsSpelledOnce(unittest.TestCase):
    """Each surface names the corpus the way `support.py` resolves it (#43)."""

    def test_the_compose_file_mounts_the_corpus_the_tests_resolve(self) -> None:
        found = mount_lines(COMPOSE.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(target for _, target in found),
            sorted(MOUNT_TARGETS),
            "container/compose.yaml no longer mounts exactly §10.4's three paths",
        )
        for source, target in found:
            with self.subTest(target=target):
                self.assertEqual(
                    source,
                    expected_source(MOUNT_TARGETS[target]),
                    f"container/compose.yaml mounts {target} from {source!r}; the corpus keeps it at "
                    f"{expected_source(MOUNT_TARGETS[target])!r} (machine-prep.md §6). A missing bind source is "
                    "not an error — Docker creates a directory there and the container reports "
                    "KEY_ABSENT for a key that was mounted.",
                )

    def test_the_deployment_chapter_spells_the_same_mounts(self) -> None:
        section = deployment_section()
        found = mount_lines(section)
        counts: dict[str, int] = {}
        for source, target in found:
            counts[target] = counts.get(target, 0) + 1
            with self.subTest(target=target):
                self.assertEqual(
                    source,
                    expected_source(MOUNT_TARGETS[target]),
                    f"§10.4 mounts {target} from {source!r}, which is not the corpus spelling. "
                    "The chapter and container/compose.yaml are one text (test_compose.py), so "
                    "both move together.",
                )
        for target in MOUNT_TARGETS:
            with self.subTest(missing=target):
                self.assertGreaterEqual(
                    counts.get(target, 0),
                    3,
                    f"§10.4 carries {target} {counts.get(target, 0)} time(s): the YAML block and "
                    "the two `docker run` equivalents are the three places a reader deploys from",
                )

    def test_the_ui_prints_the_same_mount_lines(self) -> None:
        found = ui_mount_lines()
        self.assertEqual(
            sorted(found), sorted(UI_MOUNT_LINES), "mounts.ts no longer carries the four mount lines"
        )
        for key, relative in UI_MOUNT_LINES.items():
            with self.subTest(line=key):
                self.assertEqual(
                    found[key],
                    expected_source(relative),
                    f"mounts.ts's {key} line names {found[key]!r}. ADR-0012 requires the UI to print "
                    "the deployment doc's mount line verbatim (#13 item 8), and the doc's line is "
                    f"{expected_source(relative)!r}.",
                )

    def sources(self) -> dict[str, list[str]]:
        """Every host source each surface prints, so prose cannot be mistaken for a mount."""
        return {
            "container/compose.yaml": [
                source for source, _ in mount_lines(COMPOSE.read_text(encoding="utf-8"))
            ],
            "docs/spec/10-deployment.md §10.4": [
                source for source, _ in mount_lines(deployment_section())
            ],
            "container/web/src/lib/mounts.ts": list(ui_mount_lines().values()),
        }

    def test_no_mount_line_quotes_its_own_path(self) -> None:
        # `"$HOME/clone/Amiibo/'!Essential Files'"` is one double-quoted string: the single
        # quotes are literal characters, so the path handed to Docker carries two apostrophes
        # and exists nowhere — #43's silent-directory failure, spelled with punctuation.
        for name, sources in self.sources().items():
            with self.subTest(surface=name):
                self.assertTrue(sources, f"{name}: no mount lines found — the guard went blind")
            for source in sources:
                with self.subTest(surface=name, source=source):
                    self.assertNotIn(
                        "'", source, f"{name} quotes inside its own double-quoted path: {source!r}"
                    )
                    self.assertFalse(
                        source.startswith("/"),
                        f"{name} names an absolute host path: {source!r}; "
                        "docs/references.md requires the `$REFERENCE_ROOT` spelling",
                    )

    def test_the_substitution_resolves_to_the_reference_root_the_tests_use(self) -> None:
        # `:-` is the half of the substitution Compose's parse-time interpolation does check:
        # unset takes the default, and the default is what this suite resolves the corpus with.
        with mock.patch.dict(os.environ):
            os.environ.pop("REFERENCE_ROOT", None)
            self.assertEqual(
                resolved(f"{ROOT_SUBSTITUTION}/a"),
                support.reference_root() / "a",
                "Compose's `:-` default and support.reference_root() name different roots",
            )

        # The half it does not check: a set-but-wrong root is interpolated just as happily,
        # which is why §10.4's preflight exists (the `:-` form substitutes, never fails).
        with mock.patch.dict(os.environ, {"REFERENCE_ROOT": "/tmp/ns2-corpus-probe"}):
            for target, relative in MOUNT_TARGETS.items():
                with self.subTest(target=target):
                    self.assertEqual(
                        resolved(expected_source(relative)),
                        Path("/tmp/ns2-corpus-probe") / relative,
                    )

    def test_the_guard_rejects_the_spellings_it_replaced(self) -> None:
        # The two lines #43 was measured with. The guard above is only worth having if it is
        # not vacuous, and these are exactly what it exists to catch.
        measured = (
            '-v "$HOME/clone/Amiibo/!Essential Files/key_retail.bin":/keys/key_retail.bin:ro',
            "-v \"$HOME/clone/Amiibo/'!Essential Files'\":/keys/:ro",
        )
        for line in measured:
            with self.subTest(line=line):
                self.assertNotIn(expected_source(support.REFERENCE_KEY_RELATIVE), line)
        self.assertEqual(
            mount_lines(measured[0]),
            [("$HOME/clone/Amiibo/!Essential Files/key_retail.bin", "/keys/key_retail.bin")],
            "the old key line still parses, so the mismatch above is the path and not the shape",
        )


if __name__ == "__main__":
    unittest.main()
