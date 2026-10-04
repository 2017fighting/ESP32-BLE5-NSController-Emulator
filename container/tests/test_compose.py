#!/usr/bin/env python3
"""The deployment contract: §10.4's block and `container/compose.yaml` are one text.

Issue #42: the committed compose file could not build its own image. §10.4's YAML
block is a *template* whose `image: ghcr.io/<owner>/ns2-controller:latest` reads as
"substitute your own", and genericising that into an executable file turned a
placeholder into a syntax error — `<` and `>` are not in Docker's reference character
set, and Compose hands `image:` to the builder as `-t`, so the build died before it
started. The fix is a Compose substitution whose default is the published name — the name
the research record carries literally (``container-and-web-ui.md`` §4), kept because §10.4
says so, not because this file reads the record.

What this file guards is the half a later edit breaks silently: §10.4 owns the deployment
facts, so its block **is** the file's own bytes, and a legal reference is what makes the
file executable at all. §00's amendment mechanics ask for a machine-checkable companion
wherever one is possible (rule 4), and both halves are text properties — so the guard is
hermetic: no Docker, no network, no bench host.

The macos override's "changes only the `devices:` mapping" claim (§10.4) is left
unguarded on purpose: it needs a YAML parser this suite does not depend on, and it is
not the defect this ticket fixes.

Run:  python3 -m unittest discover -s container/tests -p 'test_*.py'
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

COMPOSE = support.REPO_ROOT / "container" / "compose.yaml"
DEPLOYMENT = support.REPO_ROOT / "docs" / "spec" / "10-deployment.md"

#: The section that owns the deployment facts.
SECTION = "## 10.4 Run the container"

#: The published name `image:` defaults to, as §10.4 states it.
PUBLISHED = "ghcr.io/2017fighting/ns2-controller:latest"

# Docker's "familiar name" grammar, as `distribution/reference` states it: a path
# component is alphanumerics joined by `.`/`_`/`__`/`-`, optionally preceded by a
# registry host (with an optional port) and repository path, optionally followed by
# a tag. `<` and `>` are legal in none of those classes, which is the whole defect.
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
REFERENCE = re.compile(
    rf"^(?:{_COMPONENT}(?:\.{_COMPONENT})*(?::[0-9]+)?/)?"
    rf"{_COMPONENT}(?:/{_COMPONENT})*"
    rf"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}})?$"
)

#: Compose's substitution, with a default. A bare `${NS2_IMAGE}` is not this: it
#: resolves to the empty string when unset, which is another illegal reference.
SUBSTITUTION = re.compile(r"\$\{NS2_IMAGE:-([^}]+)\}")


def compose_service_block() -> str:
    """`container/compose.yaml` from its `services:` key to EOF, verbatim.

    The file's header comment block sits above that key by design — it is where the
    file explains itself to a reader — so the contract is the YAML from `services:`
    on, which is exactly what §10.4 shows.
    """
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[lines.index("services:"):]).rstrip("\n")


def spec_service_block() -> str:
    """§10.4's first ```yaml fence: the block a reader deploys from."""
    text = DEPLOYMENT.read_text(encoding="utf-8")
    section = text[text.index(SECTION):]
    opened = section.index("```yaml\n") + len("```yaml\n")
    return section[opened:section.index("```", opened)].rstrip("\n")


def compose_entry(key: str) -> str:
    """One service key's value as the compose file writes it (scalar keys only)."""
    for line in compose_service_block().splitlines():
        match = re.fullmatch(rf"    {key}: (\S+)", line)
        if match:
            return match.group(1)
    raise AssertionError(f"container/compose.yaml has no scalar `{key}:` under the service")


def resolved_image() -> str:
    """The reference a default build tags, or a failure naming the mistake."""
    written = compose_entry("image")
    match = SUBSTITUTION.fullmatch(written)
    if match is None:
        raise AssertionError(
            f"container/compose.yaml's image is not `${{NS2_IMAGE:-<default>}}`: {written!r}. "
            "A bare reference cannot be overridden for a bench tag; a default-less "
            "`${NS2_IMAGE}` resolves to the empty string on a host that has not set it."
        )
    return match.group(1)


class TheSpecBlockIsTheFile(unittest.TestCase):
    """§10.4's block matches `container/compose.yaml` byte for byte (issue #42)."""

    def test_the_blocks_are_identical_line_for_line(self) -> None:
        spec = spec_service_block().splitlines()
        compose = compose_service_block().splitlines()
        for number, (left, right) in enumerate(zip(spec, compose), start=1):
            if left != right:
                self.fail(
                    f"§10.4's block and container/compose.yaml disagree at line {number} "
                    f"of the block:\n  §10.4:   {left!r}\n  compose: {right!r}\n"
                    "§10.4 owns the deployment facts, so the file and the block are edited "
                    "together."
                )
        self.assertEqual(
            (len(spec), len(compose)),
            (len(compose), len(spec)),
            "§10.4's block and container/compose.yaml have different lengths",
        )


class TheImageReferenceIsLegal(unittest.TestCase):
    """The reference Compose hands the builder, and where its name comes from."""

    def test_the_default_is_a_legal_reference(self) -> None:
        image = resolved_image()
        self.assertIsNotNone(REFERENCE.match(image), f"{image!r} is not a legal image reference")
        self.assertNotIn("<", image)
        self.assertNotIn(">", image)

    def test_the_default_is_the_published_name(self) -> None:
        self.assertEqual(resolved_image(), PUBLISHED)

    def test_the_grammar_rejects_the_placeholder_it_replaced(self) -> None:
        # The guard above is only worth having if it is not vacuous. These are the two
        # illegal forms it exists to catch: the committed file's own string, which made
        # `docker compose build` exit 1 with `invalid reference format`, and the empty
        # resolution a default-less `${NS2_IMAGE}` produces on a host that has not set it.
        for illegal in ("ghcr.io/<owner>/ns2-controller:latest", ""):
            with self.subTest(reference=illegal):
                self.assertIsNone(REFERENCE.match(illegal))

    def test_the_grammar_accepts_the_two_references_this_repo_uses(self) -> None:
        for legal in (PUBLISHED, "ns2-controller:dev"):
            with self.subTest(reference=legal):
                self.assertIsNotNone(REFERENCE.match(legal))


if __name__ == "__main__":
    unittest.main()
