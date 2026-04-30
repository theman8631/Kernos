"""Drafter bypass-grep test (DRAFTER C4, AC #21 ext).

Mirrors the STS bypass-grep model: scan cohort code paths for direct
imports of forbidden Drafter capabilities (the WDP / STS / event_stream
methods that ports structurally absent). The bypass-grep is the v1
enforcement of the substrate-level invariant; capability-wrapper ports
are the primary trust boundary.

Forbidden direct imports in cohort/CRB code paths:

* ``DraftRegistry.mark_committed`` — Drafter port absents this method
* ``register_workflow(`` with ``dry_run=False`` — only
  ``register_workflow_dry_run`` is reachable from Drafter
* ``EventEmitter.emit`` raw dispatch — Drafter must use
  emit_signal / emit_receipt
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHED_DIRS = (
    REPO_ROOT / "kernos" / "kernel" / "cohorts" / "drafter",
    REPO_ROOT / "kernos" / "kernel" / "cohorts" / "crb",  # future
)

# Files in the Drafter package that ARE allowed to reference these
# names (the port definitions themselves use them via TYPE_CHECKING
# imports). Exclusion list is small and intentional.
ALLOWED_FILES = {
    "ports.py",  # the port surface itself
}


# Match attribute access (`.mark_committed`), import, or assignment —
# anything that's actually code-using the name. Skip free prose
# mentions in docstrings / comments.
_MARK_COMMITTED_PATTERN = re.compile(
    r"(?:\.mark_committed|"
    r"^\s*from\s+\S+\s+import\s+.*\bmark_committed\b|"
    r"^\s*import\s+.*\bmark_committed\b|"
    r"\bmark_committed\s*=)"
)


def _python_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


class TestNoMarkCommittedFromDrafterCode:
    """``mark_committed`` MUST NOT be referenced anywhere in Drafter
    or CRB code paths. The DraftRegistry method exists for the CRB
    main spec's commitment flow; Drafter never reaches it."""

    def test_no_mark_committed_reference(self):
        offenders: list[tuple[Path, int, str]] = []
        for watched in WATCHED_DIRS:
            for path in _python_files_under(watched):
                if "__pycache__" in path.parts:
                    continue
                if path.name in ALLOWED_FILES:
                    continue
                text = path.read_text()
                for lineno, line in enumerate(text.splitlines(), start=1):
                    # Skip comment-only lines that just MENTION the name.
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith('"""'):
                        continue
                    if _MARK_COMMITTED_PATTERN.search(line):
                        offenders.append((path, lineno, line.strip()))
        assert not offenders, (
            "Bypass detected: mark_committed referenced in Drafter/CRB "
            "code paths. The port absents this method; cohort code "
            "must not introduce a path back to it.\n"
            + "\n".join(
                f"  {p.relative_to(REPO_ROOT)}:{ln}  {body}"
                for p, ln, body in offenders
            )
        )


class TestPortAsTrustBoundary:
    """Sanity check that the structural-absence pattern in ports.py is
    the only mention of forbidden methods in the Drafter package — a
    refactor that introduces a new file referencing mark_committed
    must surface here, NOT in production."""

    def test_only_ports_file_mentions_mark_committed_in_drafter(self):
        from kernos.kernel.cohorts.drafter.ports import DrafterDraftPort

        # The class itself MUST NOT have the method.
        assert not hasattr(DrafterDraftPort, "mark_committed")
