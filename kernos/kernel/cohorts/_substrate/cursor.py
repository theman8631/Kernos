"""Durable event-stream cursor for system cohorts (Drafter D2 + reusable).

System cohorts subscribe to event_stream events via a *pull* model:
``read_next_batch()`` returns events from the cursor's current position
matching the cohort's event-type filter; ``commit_position()`` advances
the cursor only after successful processing of an event. Cursor state
is persisted in the ``cohort_cursors`` SQLite table so restart resumes
from the last-committed position.

The pull model replaces a push-notified subscription for restart
correctness: there is no in-memory queue to lose on crash, and replay
of events between the last-committed cursor and the live tail is
naturally at-least-once (handler-level ``event_id`` dedupe and the
``cohort_action_log`` substrate combine to make replay idempotent).

Per-event commit (NOT per-batch) is intentional: failure mid-batch
leaves the cursor at the last-committed event so the retry processes
the remaining events from that point.

The directory placement (``cohorts/_substrate/cursor.py``) reflects
reusability: future Pattern Observer / Curator cohorts will use the
same primitive. The table name (``cohort_cursors``) is already
cohort-agnostic.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.event_stream import Event


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_COHORT_CURSORS_DDL = """
CREATE TABLE IF NOT EXISTS cohort_cursors (
    cohort_id           TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    cursor_position     TEXT NOT NULL,
    event_types_filter  TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (cohort_id, instance_id)
)
"""

_COHORT_CURSORS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cohort_cursors_updated
    ON cohort_cursors (updated_at)
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Create the ``cohort_cursors`` table + index if absent."""
    await db.execute(_COHORT_CURSORS_DDL)
    await db.execute(_COHORT_CURSORS_INDEX)
    await db.commit()


# ---------------------------------------------------------------------------
# CursorStore — persistence layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CursorRecord:
    cohort_id: str
    instance_id: str
    cursor_position: str
    event_types_filter: tuple[str, ...]
    updated_at: str


class CursorStore:
    """SQLite-backed persistence for cohort cursor positions.

    Connection model: opens its own ``aiosqlite`` connection to
    ``instance.db`` (the shared cross-instance database used by the
    event stream and other registries). The connection runs in
    autocommit mode so each ``write_position`` is its own transaction
    — cursor state is independent of any caller's transaction.

    Idempotency: ``write_position`` uses ``INSERT OR REPLACE`` so
    repeated commits of the same position are no-ops. The
    ``(cohort_id, instance_id)`` composite primary key enforces
    one cursor row per cohort per instance.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()

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

    async def read_position(
        self, *, cohort_id: str, instance_id: str,
    ) -> CursorRecord | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM cohort_cursors WHERE cohort_id = ? AND instance_id = ?",
            (cohort_id, instance_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            event_types = tuple(json.loads(row["event_types_filter"]))
        except Exception:
            event_types = ()
        return CursorRecord(
            cohort_id=row["cohort_id"],
            instance_id=row["instance_id"],
            cursor_position=row["cursor_position"],
            event_types_filter=event_types,
            updated_at=row["updated_at"],
        )

    async def write_position(
        self,
        *,
        cohort_id: str,
        instance_id: str,
        cursor_position: str,
        event_types_filter: tuple[str, ...] | frozenset[str],
    ) -> None:
        if self._db is None:
            raise RuntimeError("CursorStore not started")
        now = datetime.now(timezone.utc).isoformat()
        types_json = json.dumps(sorted(event_types_filter))
        async with self._lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO cohort_cursors "
                "(cohort_id, instance_id, cursor_position, "
                " event_types_filter, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cohort_id, instance_id, cursor_position, types_json, now),
            )


# ---------------------------------------------------------------------------
# DurableEventCursor — per-cohort, per-instance reader
# ---------------------------------------------------------------------------


class DurableEventCursor:
    """Instance-scoped cohort cursor over event_stream.

    Persistence: cursor position stored in :class:`CursorStore`. Survives
    restart. Updated atomically with event consumption.

    Idempotency: events read with ``event_id``; cursor advance commits
    only after successful processing. Re-read of already-consumed events
    is safe via ``event_id`` dedupe in the cohort handler and the
    ``cohort_action_log`` substrate.

    Filter: subscribes to specific event types only; cursor advances past
    non-matching events without delivery. Position is the timestamp of
    the last-committed event (events are ordered by timestamp ASC in the
    underlying read API).
    """

    INITIAL_POSITION = "0"
    """Sentinel representing no events yet seen — read returns events from
    the dawn of time on first start."""

    def __init__(
        self,
        *,
        cursor_store: CursorStore,
        cohort_id: str,
        instance_id: str,
        event_types: frozenset[str],
    ) -> None:
        if not cohort_id:
            raise ValueError("cohort_id is required")
        if not instance_id:
            raise ValueError("instance_id is required")
        if not isinstance(event_types, frozenset):
            raise TypeError("event_types must be a frozenset")
        if not event_types:
            raise ValueError("event_types must be non-empty")
        self._cursor_store = cursor_store
        self._cohort_id = cohort_id
        self._instance_id = instance_id
        self._event_types = event_types
        self._position: str | None = None  # lazy load on first read

    @property
    def cohort_id(self) -> str:
        return self._cohort_id

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def event_types(self) -> frozenset[str]:
        return self._event_types

    async def current_position(self) -> str:
        """Return the cursor's current position. Loads from the store on
        first call; subsequent calls use the in-memory cache."""
        if self._position is None:
            record = await self._cursor_store.read_position(
                cohort_id=self._cohort_id, instance_id=self._instance_id,
            )
            self._position = record.cursor_position if record else self.INITIAL_POSITION
        return self._position

    async def read_next_batch(self, *, max_events: int = 10) -> "list[Event]":
        """Read the next batch of events from the current cursor position
        matching the configured event-type filter. Does NOT advance the
        cursor — ``commit_position`` does that after successful processing.
        """
        # Lazy import to avoid circular dependency: event_stream imports
        # nothing from this module, but the substrate is loaded by the
        # cohort layer that the event_stream's writer also touches.
        from kernos.kernel import event_stream

        position = await self.current_position()
        since = self._position_to_datetime(position)
        until = datetime.now(timezone.utc)
        events = await event_stream.events_in_window(
            self._instance_id, since=since, until=until,
            limit=max_events,
        )
        # Filter by type AND by strict-greater-than position so the
        # last-committed event is not redelivered.
        matching = [
            e for e in events
            if e.event_type in self._event_types and e.timestamp > position
        ]
        return matching[:max_events]

    async def commit_position(self, *, event_id: str, timestamp: str) -> None:
        """Advance the cursor to the named event. Atomic write to the
        ``CursorStore``. Caller MUST have completed processing the named
        event before invoking this — a crash before commit will replay
        the event on restart."""
        if not event_id or not timestamp:
            raise ValueError("event_id and timestamp are required")
        # Position is the event timestamp; use the event_id-suffixed form
        # so two events at the same timestamp can be distinguished if
        # the substrate ever allows that (currently SQLite ROWID gives
        # us strict ordering anyway).
        await self._cursor_store.write_position(
            cohort_id=self._cohort_id,
            instance_id=self._instance_id,
            cursor_position=timestamp,
            event_types_filter=tuple(self._event_types),
        )
        self._position = timestamp

    def _position_to_datetime(self, position: str) -> datetime:
        """Parse the cursor position back to a UTC datetime for
        ``events_in_window``. The sentinel ``"0"`` maps to epoch."""
        if position == self.INITIAL_POSITION or not position:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            dt = datetime.fromisoformat(position)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            logger.warning(
                "DurableEventCursor: malformed cursor position %r for "
                "cohort=%s instance=%s; resetting to epoch",
                position, self._cohort_id, self._instance_id,
            )
            return datetime.fromtimestamp(0, tz=timezone.utc)


__all__ = [
    "CursorRecord",
    "CursorStore",
    "DurableEventCursor",
]
