"""Durable per-instance event stream — SQLite-backed append-only timeline.

EVENT-STREAM-TO-SQLITE. A single queryable timeline of every meaningful
event across Kernos subsystems: relational dispatch, tool calls, gate
verdicts, compaction, plan step transitions, friction observations.

Design contract:

* :func:`emit` is fire-and-forget — callers append to an in-memory queue
  and return immediately. A background writer task flushes to SQLite
  every 2 seconds or when the queue reaches 100 events (whichever
  first). Ungraceful crash may lose up to 2 seconds of in-flight events.
* Shutdown drain: :func:`stop_writer` flushes the pending queue before
  returning, so a clean Kernos shutdown loses no events.
* Events are append-only. No updates, no deletes outside a future
  retention-eviction path (out of scope for this batch; 90-day default
  documented).
* Multi-tenancy: every query requires ``instance_id`` and returns only
  that instance's events.
* Name note: this module coexists with the older ``kernos.kernel.events``
  (``EventStream`` class, typed event taxonomy). The older layer stays
  untouched; this module is the new unified timeline V2's Cognition
  Kernel will consume.

Not part of this batch: consumers (reflection pass, situation model,
cross-member analytics), retention-eviction, cross-instance federation.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

logger = logging.getLogger(__name__)


DEFAULT_FLUSH_INTERVAL_S = 2.0
DEFAULT_FLUSH_THRESHOLD = 100


# ---------------------------------------------------------------------------
# Post-flush hook (WORKFLOW-LOOP-PRIMITIVE C1)
#
# Hooks fire after a successful SQLite batch write + commit, so they
# only see durably-persisted events. emit() is unchanged — its
# fire-and-forget contract is preserved; emit never awaits a hook.
#
# Execution scope: hooks run wherever _flush_once() runs successfully.
# That includes the writer task's periodic flush (the common path),
# the threshold-trigger background task spawned by emit(), the read
# APIs' pre-read flush, and explicit flush_now() / stop_writer drains.
# A slow hook therefore can slow callers that explicitly await a flush
# (reads, drain) — but it cannot affect emit's fast-path latency
# because emit either enqueues-and-returns or schedules the flush as
# a task.
#
# Failure isolation invariant: exceptions raised by any hook are
# caught + logged inside _fire_post_flush_hooks and MUST NOT propagate
# into event_stream's flush path or block other hooks. Durable event
# persistence stays independent of hook code health.
#
# The hook registry is module-level so multiple subsystems (trigger
# registry being the first; reflection-pass / improvement-loop in
# future specs) can attach without coordinating through the writer
# singleton's internal state.
# ---------------------------------------------------------------------------


PostFlushHook = Callable[[list["Event"]], Awaitable[None] | None]
"""(events_just_flushed) → optionally awaitable. Hook receives the
batch of events that were just durably persisted. Return value is
ignored; raising is caught + logged."""


_POST_FLUSH_HOOKS: list[PostFlushHook] = []


def register_post_flush_hook(hook: PostFlushHook) -> None:
    """Register a callback that fires after a successful SQLite flush.

    The callback receives the list of Events that were just durably
    persisted in that flush. Multiple hooks may register; they fire in
    registration order. Each hook is wrapped in a try/except so an
    exception in one does not affect the others or the writer's flush
    path.

    Hooks may be sync or async (`async def` callables awaited;
    plain callables called directly). Async hooks are the recommended
    shape for non-trivial work since the writer task is async.

    Idempotent for the same callable identity — registering the same
    hook twice still results in a single entry.
    """
    if hook in _POST_FLUSH_HOOKS:
        return
    _POST_FLUSH_HOOKS.append(hook)


def unregister_post_flush_hook(hook: PostFlushHook) -> bool:
    """Remove a previously-registered hook. Returns True if removed,
    False if it wasn't registered. Used by tests for clean teardown."""
    if hook not in _POST_FLUSH_HOOKS:
        return False
    _POST_FLUSH_HOOKS.remove(hook)
    return True


def _registered_post_flush_hooks() -> tuple[PostFlushHook, ...]:
    """Snapshot of current hooks. Test-only inspection surface; not
    part of the public API."""
    return tuple(_POST_FLUSH_HOOKS)


async def _fire_post_flush_hooks(batch: list["Event"]) -> None:
    """Invoke each registered hook with the freshly-flushed batch.

    Failure-isolation contract: exceptions are caught + logged with
    enough context to diagnose the offending hook; they do NOT
    propagate. Async hooks are awaited; sync hooks are called.
    """
    if not _POST_FLUSH_HOOKS:
        return
    # Snapshot the registry so a hook that re-registers (or another
    # task that mutates the list during iteration) doesn't disturb
    # this firing pass.
    hooks_snapshot = tuple(_POST_FLUSH_HOOKS)
    for hook in hooks_snapshot:
        try:
            result = hook(batch)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "EVENT_STREAM_POST_FLUSH_HOOK_FAILED hook=%s error=%s",
                getattr(hook, "__qualname__", repr(hook)),
                exc,
                exc_info=True,
            )

#: Retention window documented in install/architecture docs. Eviction is
#: a separate batch; this constant is currently informational only.
RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """A single event on the stream."""

    event_id: str
    instance_id: str
    timestamp: str
    event_type: str
    payload: dict[str, Any]
    member_id: str | None = None
    space_id: str | None = None
    correlation_id: str | None = None

    def to_row(self) -> tuple:
        return (
            self.event_id, self.instance_id, self.member_id, self.space_id,
            self.timestamp, self.event_type, json.dumps(self.payload),
            self.correlation_id,
        )

    @classmethod
    def from_row(cls, row) -> "Event":
        """Rehydrate from a DB row. Accepts aiosqlite.Row or a plain tuple."""
        try:
            payload_raw = row["payload"]
        except (KeyError, IndexError, TypeError):
            payload_raw = row[6]
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except Exception:
            payload = {}
        # Row-access: aiosqlite.Row supports both __getitem__ by name and
        # positional index. Use names for clarity.
        try:
            return cls(
                event_id=row["event_id"],
                instance_id=row["instance_id"],
                member_id=row["member_id"],
                space_id=row["space_id"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                payload=payload,
                correlation_id=row["correlation_id"],
            )
        except Exception:
            # Positional fallback
            return cls(
                event_id=row[0], instance_id=row[1], member_id=row[2],
                space_id=row[3], timestamp=row[4], event_type=row[5],
                payload=payload, correlation_id=row[7],
            )


# ---------------------------------------------------------------------------
# Writer singleton — module-level for the fire-and-forget shape
# ---------------------------------------------------------------------------


class _EventWriter:
    """Background writer — owns the queue, the flusher task, the DB conn."""

    def __init__(self) -> None:
        self._queue: list[Event] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._flush_interval_s = DEFAULT_FLUSH_INTERVAL_S
        self._flush_threshold = DEFAULT_FLUSH_THRESHOLD

    async def start(
        self,
        data_dir: str,
        *,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        """Open the DB, ensure schema, start the flusher task."""
        if self._task is not None:
            return  # already started — idempotent
        self._flush_interval_s = flush_interval_s
        self._flush_threshold = flush_threshold
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="event_stream_writer")

    async def stop(self) -> None:
        """Signal stop, drain the queue, close the connection."""
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("EVENT_STREAM_STOP_TIMEOUT: forcing cancel")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        # Final drain of anything the run loop didn't pick up
        await self._flush_once()
        if self._db is not None:
            await self._db.close()
            self._db = None

    def enqueue(self, event: Event) -> None:
        """Synchronous enqueue — the API surface of the fire-and-forget contract."""
        # Bounded queue: reject beyond a very generous ceiling rather than
        # grow unbounded if the writer is stalled or not started.
        if len(self._queue) >= 10_000:
            logger.warning("EVENT_STREAM_QUEUE_OVERFLOW: dropping event %s", event.event_type)
            return
        self._queue.append(event)

    async def _run(self) -> None:
        """Flush loop — every flush_interval or on threshold."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._flush_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            # Either stop was set or interval elapsed — flush
            try:
                await self._flush_once()
            except Exception as exc:
                logger.warning("EVENT_STREAM_FLUSH_FAILED: %s", exc)

    async def _flush_once(self) -> None:
        """Take the queue snapshot, write it to SQLite, clear."""
        if self._db is None:
            return
        if not self._queue:
            return
        async with self._lock:
            batch = list(self._queue)
            self._queue.clear()
        if not batch:
            return
        rows = [e.to_row() for e in batch]
        try:
            await self._db.executemany(
                "INSERT INTO events "
                "(event_id, instance_id, member_id, space_id, timestamp, "
                " event_type, payload, correlation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await self._db.commit()
        except Exception as exc:
            logger.warning(
                "EVENT_STREAM_WRITE_FAILED: batch=%d error=%s", len(rows), exc,
            )
            # Put them back at the front so we retry next flush
            async with self._lock:
                self._queue = batch + self._queue
            return
        # Successful durable persist — fire post-flush hooks. Hook
        # exceptions are caught inside _fire_post_flush_hooks so they
        # cannot disturb the writer task's flush path.
        await _fire_post_flush_hooks(batch)

    async def read_db(self) -> aiosqlite.Connection | None:
        """Return the live DB connection for queries. Used by read functions."""
        return self._db

    @property
    def queue_depth(self) -> int:
        return len(self._queue)


_WRITER = _EventWriter()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Create the events table + indices if absent."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id        TEXT PRIMARY KEY,
            instance_id     TEXT NOT NULL,
            member_id       TEXT,
            space_id        TEXT,
            timestamp       TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            payload         TEXT NOT NULL,
            correlation_id  TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_ts "
        "ON events(instance_id, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_member_ts "
        "ON events(instance_id, member_id, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_type_ts "
        "ON events(instance_id, event_type, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_correlation "
        "ON events(correlation_id)"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def start_writer(
    data_dir: str,
    *,
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
) -> None:
    """Start the background writer. Idempotent."""
    await _WRITER.start(
        data_dir,
        flush_interval_s=flush_interval_s,
        flush_threshold=flush_threshold,
    )


async def stop_writer() -> None:
    """Drain the queue and close the DB. Safe to call without start."""
    await _WRITER.stop()


async def flush_now() -> None:
    """Force a flush — used by tests and explicit checkpoint callers."""
    await _WRITER._flush_once()


def queue_depth() -> int:
    """Current in-memory queue depth — for tests and diagnostics."""
    return _WRITER.queue_depth


async def emit(
    instance_id: str,
    event_type: str,
    payload: dict | None = None,
    *,
    member_id: str | None = None,
    space_id: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Enqueue an event for write. Returns immediately; actual write is batched.

    This is the canonical emission entry point. Callers do not await disk
    I/O. An ungraceful crash may lose up to ``flush_interval_s`` seconds
    of in-flight events — this tradeoff is documented in the architecture
    page and intentional for write-path performance.

    Over-threshold enqueue triggers an opportunistic background flush
    without blocking the caller.
    """
    event = Event(
        event_id=str(uuid.uuid4()),
        instance_id=instance_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_type=event_type,
        payload=payload or {},
        member_id=member_id,
        space_id=space_id,
        correlation_id=correlation_id,
    )
    _WRITER.enqueue(event)
    # Opportunistic threshold flush — don't block the caller but do
    # schedule a flush so queue doesn't linger over the threshold.
    if _WRITER.queue_depth >= _WRITER._flush_threshold:
        asyncio.create_task(_WRITER._flush_once())


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


async def events_for_member(
    instance_id: str,
    member_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    event_types: list[str] | None = None,
    limit: int = 1000,
) -> list[Event]:
    """Events for a single member in ascending timestamp order."""
    db = await _WRITER.read_db()
    if db is None:
        return []
    # Ensure pending writes land before we read.
    await _WRITER._flush_once()
    clauses = ["instance_id = ?", "member_id = ?"]
    args: list[Any] = [instance_id, member_id]
    if since:
        clauses.append("timestamp >= ?")
        args.append(since.isoformat())
    if until:
        clauses.append("timestamp <= ?")
        args.append(until.isoformat())
    if event_types:
        placeholders = ",".join("?" * len(event_types))
        clauses.append(f"event_type IN ({placeholders})")
        args.extend(event_types)
    args.append(limit)
    query = (
        "SELECT * FROM events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp ASC LIMIT ?"
    )
    async with db.execute(query, args) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


async def events_in_window(
    instance_id: str,
    since: datetime,
    until: datetime,
    *,
    limit: int = 1000,
) -> list[Event]:
    """All events for an instance in a time window, ascending."""
    db = await _WRITER.read_db()
    if db is None:
        return []
    await _WRITER._flush_once()
    async with db.execute(
        "SELECT * FROM events WHERE instance_id = ? "
        "AND timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp ASC LIMIT ?",
        (instance_id, since.isoformat(), until.isoformat(), limit),
    ) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


async def events_by_correlation(
    instance_id: str,
    correlation_id: str,
) -> list[Event]:
    """All events for a given correlation id within an instance, ascending."""
    db = await _WRITER.read_db()
    if db is None:
        return []
    await _WRITER._flush_once()
    async with db.execute(
        "SELECT * FROM events WHERE instance_id = ? AND correlation_id = ? "
        "ORDER BY timestamp ASC",
        (instance_id, correlation_id),
    ) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Test helpers — explicit reset for isolation in the test suite
# ---------------------------------------------------------------------------


async def _reset_for_tests() -> None:
    """Tear down the writer singleton so the next test can start clean."""
    await stop_writer()
    # Rebind — previous instance kept its state dicts, which is fine for
    # idempotent restart, but tests want a perfectly clean slate.
    global _WRITER
    _WRITER = _EventWriter()
    # Clear post-flush hook registrations too — a hook left over from
    # an earlier test would otherwise fire on subsequent test flushes
    # and silently mutate state across tests.
    _POST_FLUSH_HOOKS.clear()


__all__ = [
    "Event",
    "PostFlushHook",
    "RETENTION_DAYS",
    "emit",
    "events_by_correlation",
    "events_for_member",
    "events_in_window",
    "flush_now",
    "queue_depth",
    "register_post_flush_hook",
    "start_writer",
    "stop_writer",
    "unregister_post_flush_hook",
]
