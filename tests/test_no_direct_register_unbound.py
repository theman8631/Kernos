"""Bypass-grep test (STS C3, AC #21).

Scans cohort and CRB code paths for direct imports of WLP's
underscore-prefixed registration entry point. The spec's enforcement
model: production code goes through SubstrateTools.register_workflow,
which binds an approval event before reaching ``_register_workflow_unbound``.
A direct import in cohort or CRB code is a bypass — the test fails on
match.

This is a structural pin without a lint framework. It is cheap, runs in
the standard pytest suite, and catches the failure mode the spec is
designed to prevent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHED_DIRS = (
    REPO_ROOT / "kernos" / "kernel" / "cohorts",
    REPO_ROOT / "kernos" / "kernel" / "crb",
)
# Patterns that indicate direct WLP-unbound usage. The bypass-grep test
# rejects all of:
# - ``from kernos.kernel.workflows.workflow_registry import _register_workflow_unbound``
# - ``import _register_workflow_unbound`` (any form)
# - ``WorkflowRegistry._register_workflow_unbound`` (attribute access)
# - ``._register_workflow_unbound`` (method call on a bound instance)
_BYPASS_PATTERN = re.compile(r"_register_workflow_unbound")
# Same idea for the substrate-internal envelope enqueue path: cohort
# and CRB code MUST go through a registered EventEmitter, NEVER reach
# for ``_enqueue_with_envelope`` or construct ``EventEmitter`` directly.
_ENVELOPE_BYPASS_PATTERNS = (
    re.compile(r"_enqueue_with_envelope"),
    re.compile(r"\bEventEmitter\s*\("),  # direct construction
)


def _python_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


class TestNoDirectUnboundRegistration:
    """AC #21: cohort/CRB code paths must not import the WLP underscore
    method directly."""

    def test_no_direct_register_unbound_in_cohorts_or_crb(self):
        offenders: list[tuple[Path, int, str]] = []
        for watched in WATCHED_DIRS:
            for path in _python_files_under(watched):
                # The pyc/cache should never contain Python source, but
                # belt-and-suspenders skip __pycache__.
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text()
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if _BYPASS_PATTERN.search(line):
                        offenders.append((path, lineno, line.strip()))
        assert not offenders, (
            "Bypass detected: direct use of "
            "_register_workflow_unbound from cohort/CRB code paths.\n"
            "Production code must go through SubstrateTools.register_workflow.\n"
            + "\n".join(
                f"  {p.relative_to(REPO_ROOT)}:{ln}  {body}"
                for p, ln, body in offenders
            )
        )


class TestNoDirectEnvelopeBypass:
    """Defense-in-depth on top of the EventEmitter token gate: cohort
    and CRB code paths must not construct EventEmitter directly or
    call _enqueue_with_envelope. The token gate raises RuntimeError on
    direct construction at runtime; this static check catches the
    bypass at code-review time."""

    def test_no_direct_envelope_bypass_in_cohorts_or_crb(self):
        offenders: list[tuple[Path, int, str]] = []
        for watched in WATCHED_DIRS:
            for path in _python_files_under(watched):
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text()
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for pattern in _ENVELOPE_BYPASS_PATTERNS:
                        if pattern.search(line):
                            offenders.append((path, lineno, line.strip()))
        assert not offenders, (
            "Bypass detected: direct EventEmitter construction or "
            "_enqueue_with_envelope use from cohort/CRB code paths.\n"
            "Production code must register through "
            "EmitterRegistry.register(source_module=...).\n"
            + "\n".join(
                f"  {p.relative_to(REPO_ROOT)}:{ln}  {body}"
                for p, ln, body in offenders
            )
        )


class TestWatcherCoversCRBWhenItLands:
    """Sanity check that the bypass-grep watch list includes the
    expected paths. Catches the failure mode where a future refactor
    moves the cohort directory and the bypass test stops scanning the
    real code."""

    def test_cohorts_dir_in_watch_list(self):
        cohorts_dir = REPO_ROOT / "kernos" / "kernel" / "cohorts"
        assert cohorts_dir in WATCHED_DIRS
        assert cohorts_dir.exists(), (
            "kernos/kernel/cohorts/ should exist; if it has been moved "
            "or renamed, update WATCHED_DIRS in this test"
        )

    def test_crb_dir_in_watch_list(self):
        # CRB lands in a future spec; the watch list pre-emptively
        # includes it so the bypass test starts catching the moment
        # CRB code lands.
        crb_dir = REPO_ROOT / "kernos" / "kernel" / "crb"
        assert crb_dir in WATCHED_DIRS
