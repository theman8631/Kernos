"""Draft registry — persistent, conversational workflow drafts.

WDP C1: schema + ``WorkflowDraft`` dataclass + ``create_draft`` +
``get_draft``. C2 adds the state-machine mutations
(``update_draft`` / ``mark_committed`` / ``abandon_draft``) +
envelope validation + concurrency. C3 adds ``list_drafts`` +
``cleanup_abandoned_older_than`` + the live sweep.

Design notes:

* **Composite primary key ``(instance_id, draft_id)``** — DAR's
  pattern. Two instances may both have a ``draft-001`` without
  collision. Cross-instance lookups return ``None`` / empty.
* **NOT NULL defaults** for ``aliases`` (``'[]'``) and
  ``partial_spec_json`` (``'{}'``) so the Python class never
  holds None for those fields. Schema rejects NULL inserts at
  the SQL layer (AC #20).
* **All public methods are keyword-only with ``instance_id`` as
  the first declared keyword parameter** (AC #19) — prevents
  positional inversion bugs across multi-tenant calls.
* **Optimistic concurrency** via a ``version`` column. C2's
  mutation methods take ``expected_version`` and increment
  atomically; mismatches raise ``DraftConcurrentModification``.
* **Event emission is optional**: an injected callable
  ``(event_type, payload, instance_id) -> Awaitable[None]``.
  ``None`` means no-op (test-fixture path). Production wires the
  emitter to ``event_stream.emit`` at engine bring-up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

from kernos.kernel.drafts.errors import (
    DraftAliasCollision,
    DraftError,
    DraftNotFound,
)

logger = logging.getLogger(__name__)


VALID_DRAFT_STATUSES = frozenset({
    "shaping", "blocked", "ready", "committed", "abandoned",
})

TERMINAL_STATUSES = frozenset({"committed", "abandoned"})


# Event emitter signature: (event_type, payload, *, instance_id) →
# Awaitable[None] | None. Tests pass None and skip; production
# wires this to a closure around event_stream.emit.
EventEmitter = Callable[..., "Awaitable[None] | None"]


# ---------------------------------------------------------------------------
# WorkflowDraft dataclass
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDraft:
    """In-memory representation of a row in ``workflow_drafts``.

    ``aliases`` and ``partial_spec_json`` are NEVER None — schema
    NOT NULL with safe defaults (Kit edit, v1 → v2). The Python
    class enforces the same: empty list / empty dict, never None.
    """

    draft_id: str
    instance_id: str
    status: str = "shaping"
    home_space_id: str | None = None
    display_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    intent_summary: str = ""
    partial_spec_json: dict = field(default_factory=dict)
    resolution_notes: str | None = None
    source_thread_id: str | None = None
    version: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_touched_at: str = ""
    committed_workflow_id: str | None = None

    def to_row(self) -> tuple:
        return (
            self.draft_id,
            self.instance_id,
            self.home_space_id,
            self.status,
            self.display_name,
            json.dumps(self.aliases),
            self.intent_summary,
            json.dumps(self.partial_spec_json),
            self.resolution_notes,
            self.source_thread_id,
            self.version,
            self.created_at,
            self.updated_at,
            self.last_touched_at,
            self.committed_workflow_id,
        )

    @classmethod
    def from_row(cls, row) -> "WorkflowDraft":
        try:
            aliases = json.loads(row["aliases"]) if row["aliases"] else []
        except Exception:
            aliases = []
        try:
            partial = json.loads(row["partial_spec_json"]) if row["partial_spec_json"] else {}
        except Exception:
            partial = {}
        if not isinstance(aliases, list):
            aliases = []
        if not isinstance(partial, dict):
            partial = {}
        return cls(
            draft_id=row["draft_id"],
            instance_id=row["instance_id"],
            home_space_id=row["home_space_id"],
            status=row["status"],
            display_name=row["display_name"],
            aliases=aliases,
            intent_summary=row["intent_summary"] or "",
            partial_spec_json=partial,
            resolution_notes=row["resolution_notes"],
            source_thread_id=row["source_thread_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_touched_at=row["last_touched_at"],
            committed_workflow_id=row["committed_workflow_id"],
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_DRAFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_drafts (
    draft_id            TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    home_space_id       TEXT,
    status              TEXT NOT NULL CHECK (
        status IN ('shaping','blocked','ready','committed','abandoned')
    ),
    display_name        TEXT,
    aliases             TEXT NOT NULL DEFAULT '[]',
    intent_summary      TEXT,
    partial_spec_json   TEXT NOT NULL DEFAULT '{}',
    resolution_notes    TEXT,
    source_thread_id    TEXT,
    version             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_touched_at     TEXT NOT NULL,
    committed_workflow_id TEXT,
    PRIMARY KEY (instance_id, draft_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_status
    ON workflow_drafts (instance_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_home
    ON workflow_drafts (instance_id, home_space_id);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_touched
    ON workflow_drafts (instance_id, last_touched_at);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _DRAFT_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class DraftRegistry:
    """Persistence + lifecycle layer for workflow drafts.

    All public methods are keyword-only with ``instance_id`` listed
    first (AC #19). The class follows DAR's per-subsystem
    aiosqlite connection pattern (``isolation_level=None`` for
    explicit transaction control where needed).
    """

    def __init__(
        self,
        *,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()
        self._event_emitter = event_emitter

    async def start(self, data_dir: str) -> None:
        """Open the SQLite connection and ensure schema. Idempotent."""
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- create_draft ---------------------------------------------------

    async def create_draft(
        self,
        *,
        instance_id: str,
        intent_summary: str,
        home_space_id: str | None = None,
        source_thread_id: str | None = None,
    ) -> WorkflowDraft:
        """Atomic insert. Generates ``draft_id`` (UUIDv4). Initial
        ``status='shaping'``, ``version=0``.
        ``created_at == updated_at == last_touched_at == now``.
        Emits ``draft.created`` after persistence.
        """
        if self._db is None:
            raise RuntimeError("DraftRegistry not started")
        if not instance_id:
            raise ValueError("instance_id is required")
        now = _now()
        draft = WorkflowDraft(
            draft_id=str(uuid.uuid4()),
            instance_id=instance_id,
            home_space_id=home_space_id,
            status="shaping",
            intent_summary=intent_summary,
            source_thread_id=source_thread_id,
            version=0,
            created_at=now,
            updated_at=now,
            last_touched_at=now,
        )
        async with self._lock:
            await self._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, home_space_id, status,"
                " display_name, aliases, intent_summary,"
                " partial_spec_json, resolution_notes, source_thread_id,"
                " version, created_at, updated_at, last_touched_at,"
                " committed_workflow_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                draft.to_row(),
            )
        await self._emit("draft.created", {
            "draft_id": draft.draft_id,
            "instance_id": draft.instance_id,
            "home_space_id": draft.home_space_id,
            "intent_summary": draft.intent_summary,
            "source_thread_id": draft.source_thread_id,
            "created_at": draft.created_at,
        }, instance_id=draft.instance_id)
        return draft

    # -- get_draft ------------------------------------------------------

    async def get_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
    ) -> WorkflowDraft | None:
        """Deterministic lookup. Returns the row regardless of
        status — terminal-state drafts are still readable (audit
        surfaces consume this). Cross-instance lookups return None."""
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM workflow_drafts "
            "WHERE instance_id = ? AND draft_id = ?",
            (instance_id, draft_id),
        ) as cur:
            row = await cur.fetchone()
        return WorkflowDraft.from_row(row) if row else None

    # -- event emission helper ----------------------------------------

    async def _emit(
        self, event_type: str, payload: dict, *, instance_id: str,
    ) -> None:
        """Fire-and-forget event emission. ``self._event_emitter``
        of None is the no-op test-fixture path; production wires it
        to ``event_stream.emit``. Failures are caught + logged so
        emission never breaks the caller's mutation flow."""
        if self._event_emitter is None:
            return
        try:
            result = self._event_emitter(
                event_type=event_type,
                payload=payload,
                instance_id=instance_id,
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning(
                "DRAFT_EVENT_EMIT_FAILED type=%s draft_id=%s error=%s",
                event_type, payload.get("draft_id"), exc,
            )


__all__ = [
    "DraftRegistry",
    "EventEmitter",
    "TERMINAL_STATUSES",
    "VALID_DRAFT_STATUSES",
    "WorkflowDraft",
]
