"""The in-memory plan cache and the hash contract (§8.4, §5.6, ADR-0010).

Identity is a container-owned SHA-256 over the compiled payload, truncated to
16 bytes. The cache is keyed ``(path, mtime, size)`` and lives in memory only:
a compiler or format change yields a new identity and forces a re-upload, and
nothing compiled is ever written to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from os import PathLike

from .compiler import (
    MacroRejected,
    Plan,
    PlanHashMismatch,
    assert_echo,
    compile_json,
    hash_matches,
)


@dataclass(frozen=True, slots=True)
class PlanCacheEntry:
    """One cached compile, with the file facts that keyed it."""

    plan: Plan
    path: str
    mtime_ns: int
    size: int


class PlanCache:
    """A memory-only cache of compiled plans, keyed by file identity (§8.4)."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int], PlanCacheEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, path: str | PathLike[str]) -> bool:
        return any(key[0] == os.fspath(path) for key in self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def invalidate(self, path: str | PathLike[str]) -> None:
        """Drop every entry for ``path`` (e.g. the UI's ``Rescan``)."""
        target = os.fspath(path)
        for key in [key for key in self._entries if key[0] == target]:
            del self._entries[key]

    def load(
        self,
        path: str | PathLike[str],
        *,
        capacity_bytes: int | None = None,
    ) -> Plan:
        """Compile ``path``, reusing the cached plan while the file is unchanged.

        Raises :class:`~container.ns2plan.compiler.MacroRejected` for a macro
        that does not compile; the rejected macro keeps its reason upstream and
        is never cached as a plan.
        """
        target = os.fspath(path)
        stat = os.stat(target)
        key = (target, stat.st_mtime_ns, stat.st_size)
        entry = self._entries.get(key)
        if entry is None:
            with open(target, encoding="utf-8") as handle:
                raw = handle.read()
            plan = compile_json(raw, capacity_bytes=capacity_bytes)
            entry = PlanCacheEntry(
                plan=plan,
                path=target,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
            )
            self._entries[key] = entry
        else:
            self._check_capacity(entry.plan, capacity_bytes)
        # Drop stale entries for the same path so the cache cannot grow with
        # every mtime change while the file sits in the library.
        self._forget_other_revisions(target, key)
        return entry.plan

    @staticmethod
    def _check_capacity(plan: Plan, capacity_bytes: int | None) -> None:
        if capacity_bytes is not None and plan.size > capacity_bytes:
            raise MacroRejected(
                f"plan is {plan.size} bytes, above the device capacity of "
                f"{capacity_bytes}",
                code="PLAN_TOO_LARGE",
            )

    def _forget_other_revisions(self, path: str, keep: tuple[str, int, int]) -> None:
        for key in [key for key in self._entries if key[0] == path and key != keep]:
            del self._entries[key]

    @staticmethod
    def verify_echo(sent: bytes, echoed: bytes) -> None:
        """Compare the device's echoed ``plan_hash`` against ours (§5.6).

        The device never hashes anything (ADR-0010), so this comparison is the
        only thing standing between a drifted plan and a run rendered as
        current. Raises :class:`PlanHashMismatch` when they disagree.
        """
        assert_echo(sent, echoed)

    @staticmethod
    def echo_matches(sent: bytes, echoed: bytes) -> bool:
        return hash_matches(sent, echoed)


__all__ = ["PlanCache", "PlanCacheEntry", "PlanHashMismatch"]
