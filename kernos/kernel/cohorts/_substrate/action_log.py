"""Crash-idempotent action log for cohort side effects.

A system cohort's side effects (WDP writes, signal emissions, receipts)
must be replay-safe: if the process crashes between performing a side
effect and committing the cursor that consumed the originating event,
restart will replay the event. Replay must NOT duplicate the side
effect.

The action log records each side effect keyed by

    (cohort_id, instance_id, source_event_id, action_type, target_id)

with NOT NULL ``target_id`` (Kit pin v1→v2). SQLite NULL values in
composite PKs do NOT compare equal under UNIQUE constraint semantics —
two NULL ``target_id`` rows would coexist and defeat dedupe. Every v1
action type produces a deterministic, non-null ``target_id`` (draft_id,
signal_id, or receipt_id derived from the source event + action key),
so the constraint is achievable without sentinel values.

Atomicity: ``record_and_perform`` performs the side effect AND inserts
the log row in the SAME transaction. A crash between the two does not
exist — either both happen or neither. Replay finds the log row and
returns the cached ``result_summary`` instead of re-applying.

The directory placement (``cohorts/_substrate/action_log.py``) reflects
reusable intent: future Pattern Observer / Curator cohorts can import
this primitive directly without Drafter-specific assumptions.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite

logger = logging.getLogger(__name__)


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


# Pinned set: adding a new action type is a deliberate substrate change.
# Tests (test_cohort_action_log.py) assert this exact set so a future
# refactor can't quietly extend the surface.
ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({
    "create_draft",
    "update_draft",
    "abandon_draft",
    "emit_signal",
    "emit_receipt",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ActionLogError(Exception):
    """Base for action_log errors."""


class ActionLogInvalidActionType(ActionLogError):
    """Raised when ``action_type`` is not in :data:`ALLOWED_ACTION_TYPES`."""


class ActionLogInvalidTarget(ActionLogError):
    """Raised when ``target_id`` is empty or ``None``. The substrate
    requires NOT NULL composite-key components (Kit pin v1→v2)."""


class ActionLogConflict(ActionLogError):
    """Raised when ``record_and_perform`` finds a conflicting prior result
    for the same composite key. Should never occur in production; pin
    guards against logic-bug regression."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_ACTION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS cohort_action_log (
    cohort_id        TEXT NOT NULL,
    instance_id      TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    result_summary   TEXT,
    performed_at     TEXT NOT NULL,
    PRIMARY KEY (cohort_id, instance_id, source_event_id, action_type, target_id)
)
"""

_ACTION_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cohort_action_log_event
    ON cohort_action_log (cohort_id, instance_id, source_event_id)
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(_ACTION_LOG_DDL)
    await db.execute(_ACTION_LOG_INDEX)
    await db.commit()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRecord:
    cohort_id: str
    instance_id: str
    source_event_id: str
    action_type: str
    target_id: str
    result_summary: dict
    performed_at: str

    @classmethod
    def from_row(cls, row) -> "ActionRecord":
        try:
            summary = json.loads(row["result_summary"]) if row["result_summary"] else {}
        except Exception:
            summary = {}
        return cls(
            cohort_id=row["cohort_id"],
            instance_id=row["instance_id"],
            source_event_id=row["source_event_id"],
            action_type=row["action_type"],
            target_id=row["target_id"],
            result_summary=summary,
            performed_at=row["performed_at"],
        )


# ---------------------------------------------------------------------------
# ActionLog
# ---------------------------------------------------------------------------


class ActionLog:
    """Crash-idempotent action recording for cohort side effects.

    Construction binds a ``cohort_id``; lookups and inserts are scoped
    to that cohort. The log shares ``instance.db`` with other registries
    via its own ``aiosqlite`` connection (separate from event_stream and
    workflow_registry, per the same connection-isolation pattern those
    use).
    """

    def __init__(self, *, cohort_id: str) -> None:
        if not cohort_id:
            raise ValueError("cohort_id is required")
        self._cohort_id = cohort_id
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()

    @property
    def cohort_id(self) -> str:
        return self._cohort_id

    async def start(self, data_dir: str) -> None:
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

    async def is_already_done(
        self,
        *,
        instance_id: str,
        source_event_id: str,
        action_type: str,
        target_id: str,
    ) -> ActionRecord | None:
        """Return the recorded :class:`ActionRecord` if the action has
        already been performed; ``None`` otherwise. Cohorts use this
        as an explicit "did I do this already?" check before a side
        effect; ``record_and_perform`` is the atomic-record helper.
        """
        self._validate_action_type(action_type)
        self._validate_target_id(target_id)
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM cohort_action_log WHERE "
            "cohort_id = ? AND instance_id = ? AND source_event_id = ? "
            "AND action_type = ? AND target_id = ?",
            (self._cohort_id, instance_id, source_event_id, action_type, target_id),
        ) as cur:
            row = await cur.fetchone()
        return ActionRecord.from_row(row) if row else None

    async def record_and_perform(
        self,
        *,
        instance_id: str,
        source_event_id: str,
        action_type: str,
        target_id: str,
        perform: Callable[[], Awaitable[T]],
        result_to_summary: Callable[[T], dict] | None = None,
    ) -> T:
        """Atomic: BEGIN; perform side effect; INSERT log row; COMMIT.

        On UNIQUE-constraint violation (replay of a previously-committed
        action), rolls back, looks up the prior record, and returns the
        cached ``result_summary``.

        Args:
            instance_id: scope.
            source_event_id: the event whose processing produced this
                side effect; must be stable across replay so the
                composite key collides.
            action_type: must be in :data:`ALLOWED_ACTION_TYPES`.
            target_id: deterministic identifier for the side effect's
                target (draft_id, signal_id, receipt_id). NOT NULL.
            perform: async callable producing the side effect's result.
                Called exactly once per (composite key) — never twice.
            result_to_summary: optional callable converting ``perform``'s
                return value to a JSON-serializable summary stored in
                the log row. Default: best-effort dict.

        Returns:
            The result of ``perform`` (or, on replay, a synthesized
            equivalent reconstructed from ``result_summary``).

        Raises:
            ActionLogInvalidActionType, ActionLogInvalidTarget: input
                validation.
            ActionLogConflict: a record exists but the action could not
                be skipped — should never happen in production.
        """
        self._validate_action_type(action_type)
        self._validate_target_id(target_id)
        if self._db is None:
            raise RuntimeError("ActionLog not started")
        # Fast-path replay check before grabbing the lock.
        prior = await self.is_already_done(
            instance_id=instance_id,
            source_event_id=source_event_id,
            action_type=action_type,
            target_id=target_id,
        )
        if prior is not None:
            return self._replay_return(prior)

        async with self._lock:
            # Re-check inside the lock to close the race.
            prior = await self.is_already_done(
                instance_id=instance_id,
                source_event_id=source_event_id,
                action_type=action_type,
                target_id=target_id,
            )
            if prior is not None:
                return self._replay_return(prior)

            await self._db.execute("BEGIN")
            try:
                result = await perform()
                summary = (
                    result_to_summary(result)
                    if result_to_summary is not None
                    else self._default_summary(result)
                )
                now = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    "INSERT INTO cohort_action_log "
                    "(cohort_id, instance_id, source_event_id, action_type, "
                    " target_id, result_summary, performed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._cohort_id, instance_id, source_event_id,
                        action_type, target_id, json.dumps(summary), now,
                    ),
                )
                await self._db.execute("COMMIT")
                return result
            except aiosqlite.IntegrityError:
                # Concurrent replay won the race — roll back, return prior.
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                prior = await self.is_already_done(
                    instance_id=instance_id,
                    source_event_id=source_event_id,
                    action_type=action_type,
                    target_id=target_id,
                )
                if prior is None:
                    raise ActionLogConflict(
                        f"action_log UNIQUE violation but prior record "
                        f"not found: cohort={self._cohort_id!r} "
                        f"instance={instance_id!r} "
                        f"source_event={source_event_id!r} "
                        f"action={action_type!r} target={target_id!r}"
                    )
                return self._replay_return(prior)
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    @staticmethod
    def _validate_action_type(action_type: str) -> None:
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ActionLogInvalidActionType(
                f"action_type {action_type!r} not in allowed set "
                f"{sorted(ALLOWED_ACTION_TYPES)}"
            )

    @staticmethod
    def _validate_target_id(target_id: str) -> None:
        if not target_id:
            raise ActionLogInvalidTarget(
                "target_id is required and must be non-empty (NOT NULL "
                "composite-key invariant per spec v1→v2)"
            )

    @staticmethod
    def _default_summary(result: Any) -> dict:
        """Fallback for callers that don't supply ``result_to_summary``."""
        if hasattr(result, "__dict__"):
            return {"result_repr": repr(result)}
        return {"result": result if isinstance(result, (str, int, float, bool, type(None))) else repr(result)}

    @staticmethod
    def _replay_return(prior: ActionRecord) -> Any:
        """On replay, we cannot reconstruct the live object that
        ``perform`` originally returned. Return the recorded
        ``result_summary`` so callers can detect replay. Cohorts that
        need the live object on replay must look it up themselves
        (e.g., ``DraftRegistry.get_draft(draft_id=summary['draft_id'])``).
        """
        return prior.result_summary


__all__ = [
    "ActionLog",
    "ActionLogConflict",
    "ActionLogError",
    "ActionLogInvalidActionType",
    "ActionLogInvalidTarget",
    "ActionRecord",
    "ALLOWED_ACTION_TYPES",
]
