"""Proactive Awareness — background kernel process for surfacing signals.

The AwarenessEvaluator runs on a periodic timer, checks the knowledge store
for time-anchored signals worth surfacing, and queues structured Whisper
objects. The handler injects pending whispers at session start.

The evaluator OBSERVES only — it never acts on the world.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.state import KnowledgeEntry, StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_whisper_id() -> str:
    """Generate a unique, time-sortable whisper ID."""
    ts_us = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"wsp_{ts_us}_{rand}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Whisper:
    """A structured insight the evaluator wants the agent to surface."""

    whisper_id: str              # Unique ID: "wsp_{timestamp}_{rand4}"
    insight_text: str            # Natural language framing for the agent
    delivery_class: str          # "stage" or "ambient" (no "interrupt" in 3C)
    source_space_id: str         # Context space where the signal originated
    target_space_id: str         # Where to deliver (usually source; cross-domain = active space)
    supporting_evidence: list[str]  # Underlying data for follow-up questions
    reasoning_trace: str         # Why this was surfaced (agent draws on when user asks)
    knowledge_entry_id: str      # The KnowledgeEntry that triggered this whisper
    foresight_signal: str        # Raw signal from the knowledge entry (stable — used for suppression matching)
    created_at: str              # ISO 8601 UTC
    surfaced_at: str = ""        # When the agent actually received it (empty = pending)


@dataclass
class SuppressionEntry:
    """Tracks what has been surfaced to prevent nagging."""

    whisper_id: str
    knowledge_entry_id: str      # What triggered the whisper
    foresight_signal: str        # RAW signal from KnowledgeEntry (not formatted insight_text)
    created_at: str              # When the whisper was first created
    resolution_state: str        # "surfaced" | "dismissed" | "acted_on" | "resolved"
    resolved_by: str = ""        # "user_dismissed" | "already_handled" | "entry_expired"
                                 # Note: "knowledge_updated" DELETES the entry (see Component 6)
    resolved_at: str = ""        # When resolution happened


# ---------------------------------------------------------------------------
# Tool definition for dismiss_whisper
# ---------------------------------------------------------------------------

DISMISS_WHISPER_TOOL = {
    "name": "dismiss_whisper",
    "description": (
        "Dismiss a proactive insight so it won't be surfaced again. "
        "Use when the user explicitly says they don't want to hear "
        "about this topic or have already handled it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "whisper_id": {
                "type": "string",
                "description": "The whisper ID to dismiss (from the proactive awareness block)",
            },
            "reason": {
                "type": "string",
                "enum": ["user_dismissed", "already_handled"],
                "description": "Why this whisper is being dismissed",
            },
        },
        "required": ["whisper_id"],
    },
}


# ---------------------------------------------------------------------------
# AwarenessEvaluator
# ---------------------------------------------------------------------------


class AwarenessEvaluator:
    """Background kernel process that checks for signals worth surfacing.

    Runs on a periodic timer. Produces Whisper objects.
    Does NOT call LLMs (MVP time pass). Does NOT act on the world.
    """

    def __init__(
        self,
        state: StateStore,
        events: EventStream,
        interval_seconds: int = 1800,  # 30 minutes default
    ) -> None:
        self._state = state
        self._events = events
        self._interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, tenant_id: str) -> None:
        """Start the periodic evaluator for a tenant."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(tenant_id))
        logger.info("AWARENESS: evaluator started for tenant=%s interval=%ds",
                     tenant_id, self._interval)

    async def stop(self) -> None:
        """Stop the evaluator."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AWARENESS: evaluator stopped")

    async def _run_loop(self, tenant_id: str) -> None:
        """Main loop — run evaluations on interval."""
        while self._running:
            try:
                await self._evaluate(tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("AwarenessEvaluator error: %s", e)
            await asyncio.sleep(self._interval)

    async def _evaluate(self, tenant_id: str) -> None:
        """Run all evaluation passes for a tenant."""
        whispers = await self.run_time_pass(tenant_id)

        for whisper in whispers:
            # Check suppression — don't re-surface
            if await self._is_suppressed(tenant_id, whisper):
                logger.info("AWARENESS: suppressed whisper=%s signal=%r",
                            whisper.whisper_id, whisper.insight_text[:80])
                continue

            # Save to pending queue
            await self._state.save_whisper(tenant_id, whisper)

            # Emit audit event
            try:
                await emit_event(
                    self._events,
                    EventType.PROACTIVE_INSIGHT,
                    tenant_id,
                    "awareness_evaluator",
                    payload={
                        "whisper_id": whisper.whisper_id,
                        "insight_text": whisper.insight_text,
                        "delivery_class": whisper.delivery_class,
                        "source_space_id": whisper.source_space_id,
                        "knowledge_entry_id": whisper.knowledge_entry_id,
                        "reasoning_trace": whisper.reasoning_trace,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit proactive.insight: %s", exc)

            logger.info("AWARENESS: queued whisper=%s class=%s signal=%r",
                        whisper.whisper_id, whisper.delivery_class,
                        whisper.insight_text[:80])

        # Enforce queue bound — max 10 pending whispers per tenant
        await self._enforce_queue_bound(tenant_id, max_whispers=10)

        # Periodic cleanup of old suppressions
        await self._cleanup_old_suppressions(tenant_id)

    async def run_time_pass(self, tenant_id: str) -> list[Whisper]:
        """Check for time-anchored signals worth surfacing.

        Queries knowledge entries where foresight_expires falls within
        the next 48 hours. Packages each as a whisper.
        """
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=48)

        entries = await self._state.query_knowledge_by_foresight(
            tenant_id,
            expires_before=window_end.isoformat(),
            expires_after=now.isoformat(),
        )

        whispers = []
        for entry in entries:
            # Calculate urgency from time remaining
            try:
                expires_dt = datetime.fromisoformat(entry.foresight_expires)
            except (ValueError, TypeError):
                continue
            hours_remaining = (expires_dt - now).total_seconds() / 3600

            delivery_class = "stage" if hours_remaining < 12 else "ambient"

            whisper = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=self._format_time_insight(entry, hours_remaining),
                delivery_class=delivery_class,
                source_space_id=entry.context_space or "",
                target_space_id=entry.context_space or "",  # Same space for time pass
                supporting_evidence=[
                    f"Knowledge entry: {entry.id}",
                    f"Foresight signal: {entry.foresight_signal}",
                    f"Expires: {entry.foresight_expires}",
                    f"Hours remaining: {hours_remaining:.1f}",
                ],
                reasoning_trace=(
                    f"Time pass detected: '{entry.foresight_signal}' "
                    f"expires in {hours_remaining:.1f} hours. "
                    f"Source: knowledge entry {entry.id} in {entry.context_space}."
                ),
                knowledge_entry_id=entry.id,
                foresight_signal=entry.foresight_signal,
                created_at=now.isoformat(),
            )
            whispers.append(whisper)

        logger.info("AWARENESS: time_pass entries_checked=%d whispers_produced=%d",
                     len(entries), len(whispers))

        return whispers

    def _format_time_insight(self, entry: KnowledgeEntry, hours: float) -> str:
        """Format a foresight signal into natural insight text for the agent."""
        if hours < 2:
            urgency = "very soon"
        elif hours < 6:
            urgency = "in the next few hours"
        elif hours < 24:
            urgency = "today"
        else:
            urgency = "tomorrow"

        return (
            f"Upcoming: {entry.foresight_signal}. "
            f"This is relevant {urgency} (expires in ~{hours:.0f} hours). "
            f"Related knowledge: {entry.content[:200]}"
        )

    async def _enforce_queue_bound(self, tenant_id: str, max_whispers: int = 10) -> None:
        """Trim the whisper queue to max_whispers.

        Priority: stage before ambient. Within same class, newest first.
        Excess whispers are silently dropped (not suppressed — they just
        didn't make the cut).
        """
        pending = await self._state.get_pending_whispers(tenant_id)
        if len(pending) <= max_whispers:
            return

        # Sort: stage first, then by created_at descending (newest first)
        pending.sort(key=lambda w: (
            0 if w.delivery_class == "stage" else 1,
            w.created_at,
        ))
        # Within same delivery class, we want newest first, so reverse created_at
        # Re-sort with proper key: stage before ambient, then newest first
        pending.sort(key=lambda w: (
            0 if w.delivery_class == "stage" else 1,
            # Negate time: invert ISO string is complex, just use a tuple sort
        ))
        # Simpler approach: separate stage and ambient, sort each by newest
        stage = [w for w in pending if w.delivery_class == "stage"]
        ambient = [w for w in pending if w.delivery_class != "stage"]
        stage.sort(key=lambda w: w.created_at, reverse=True)  # newest first
        ambient.sort(key=lambda w: w.created_at, reverse=True)  # newest first
        prioritized = stage + ambient

        # Keep the top max_whispers, delete the rest
        keep = set(w.whisper_id for w in prioritized[:max_whispers])
        for w in prioritized:
            if w.whisper_id not in keep:
                await self._state.delete_whisper(tenant_id, w.whisper_id)
                logger.info("AWARENESS: trimmed whisper=%s (queue bound %d)",
                            w.whisper_id, max_whispers)

    async def _is_suppressed(self, tenant_id: str, whisper: Whisper) -> bool:
        """Check if this whisper has already been surfaced or dismissed.

        Suppression is keyed to knowledge_entry_id, not insight text.
        The insight text changes every cycle (countdown updates). The
        knowledge entry ID is stable — if we already surfaced a whisper
        for this entry and nothing changed, suppress.
        """
        suppressions = await self._state.get_suppressions(
            tenant_id,
            knowledge_entry_id=whisper.knowledge_entry_id,
        )

        for s in suppressions:
            if s.resolution_state in ("surfaced", "dismissed", "acted_on"):
                return True
            if s.resolution_state == "resolved" and s.resolved_by == "entry_expired":
                return True

        return False

    async def _cleanup_old_suppressions(self, tenant_id: str) -> None:
        """Remove suppression entries older than 7 days."""
        suppressions = await self._state.get_suppressions(tenant_id)
        now = datetime.now(timezone.utc)
        removed = 0
        for s in suppressions:
            try:
                created = datetime.fromisoformat(s.created_at)
                if (now - created).total_seconds() > 7 * 86400:
                    await self._state.delete_suppression(tenant_id, s.whisper_id)
                    removed += 1
            except (ValueError, TypeError):
                continue
        if removed:
            logger.info("AWARENESS: cleanup suppressions_removed=%d", removed)
