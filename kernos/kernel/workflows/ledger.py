"""Per-workflow append-only markdown ledger.

WORKFLOW-LOOP-PRIMITIVE C5. One ledger.md per workflow at
``data/{instance_id}/workflows/{workflow_id}/ledger.md``. Each entry
captures one execution step: timestamp, execution_id, step_index,
agent_or_action, synopsis (1-3 sentences), result_summary,
kickback_if_any, references.

Cross-instance isolation pin: every read/write resolves the
absolute ledger path and verifies it falls inside
``data/{instance_id}/workflows/`` before any I/O. Attempts to
write to another instance's subtree (via ``..``, absolute paths,
symlink shenanigans) raise ``LedgerPathViolation``.

Append-only: no destructive deletions per the standing principle.
Ledger entries are appended; the file is never rewritten.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LedgerPathViolation(RuntimeError):
    """Raised when a ledger path operation would cross the
    instance-scoped subtree boundary."""


# Filesystem-safe identifiers: alphanumerics, dash, underscore.
# Anything else is rewritten to underscore for the directory name
# (the original workflow_id is preserved in the entry body).
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_segment(name: str) -> str:
    if not name:
        raise LedgerPathViolation("ledger path segment must be non-empty")
    safe = _SAFE_NAME_RE.sub("_", name)
    if safe in (".", ".."):
        raise LedgerPathViolation(f"ledger path segment {name!r} reserved")
    return safe


class WorkflowLedger:
    """Ledger surface for a single Kernos installation.

    Construction takes the data directory (e.g. ``./data``); the
    ledger writes under ``{data_dir}/{instance_id}/workflows/{workflow_id}/ledger.md``.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).resolve()

    # -- path helpers ---------------------------------------------------

    def _instance_root(self, instance_id: str) -> Path:
        if not instance_id:
            raise LedgerPathViolation("instance_id must be non-empty")
        return (self._data_dir / _safe_segment(instance_id) / "workflows").resolve()

    def ledger_path(self, instance_id: str, workflow_id: str) -> Path:
        """Resolve the ledger file path for (instance_id, workflow_id).

        Raises ``LedgerPathViolation`` if the resolution falls outside
        the instance-scoped subtree (defence against ``..`` injection,
        absolute-path workflow_ids, or symlink games)."""
        instance_root = self._instance_root(instance_id)
        path = (
            instance_root
            / _safe_segment(workflow_id)
            / "ledger.md"
        )
        resolved = path.resolve()
        # The resolved path MUST sit inside instance_root.
        try:
            resolved.relative_to(instance_root)
        except ValueError:
            raise LedgerPathViolation(
                f"ledger path {resolved!s} resolves outside the instance "
                f"subtree {instance_root!s}"
            )
        return resolved

    # -- write ----------------------------------------------------------

    async def append(
        self, instance_id: str, workflow_id: str, entry: dict,
    ) -> None:
        """Append a single entry to the ledger. Creates the parent
        directory + file on first write."""
        path = self.ledger_path(instance_id, workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = self._format_entry(entry)
        with path.open("a") as fp:
            fp.write(line)

    # -- read -----------------------------------------------------------

    async def read_all(
        self, instance_id: str, workflow_id: str,
    ) -> list[dict]:
        path = self.ledger_path(instance_id, workflow_id)
        if not path.exists():
            return []
        out: list[dict] = []
        with path.open("r") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw or not raw.startswith("- "):
                    continue
                # Each entry is "- <json-payload>" so reads stay parseable
                # while the file remains markdown-compatible.
                try:
                    out.append(json.loads(raw[2:]))
                except json.JSONDecodeError:
                    continue
        return out

    async def read_last(
        self, instance_id: str, workflow_id: str,
    ) -> dict | None:
        entries = await self.read_all(instance_id, workflow_id)
        return entries[-1] if entries else None

    # -- formatting -----------------------------------------------------

    def _format_entry(self, entry: dict) -> str:
        # Augment with timestamp if not provided so audits stay
        # self-describing.
        body = dict(entry)
        body.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
        return "- " + json.dumps(body, sort_keys=True) + "\n"


__all__ = [
    "LedgerPathViolation",
    "WorkflowLedger",
]
