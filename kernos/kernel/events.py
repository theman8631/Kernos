"""Event Stream — the kernel's nervous system.

Every component emits events. Multiple components read them. Append-only.
Immutable once written.
"""
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing (updated manually when models change)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, dict[str, float]] = {
    # USD per million tokens
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts. Returns 0.0 for unknown models."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    return (input_tokens * pricing["input"] / 1_000_000) + (
        output_tokens * pricing["output"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Event ID generation
# ---------------------------------------------------------------------------


def generate_event_id() -> str:
    """Generate a unique, time-sortable event ID.

    Format: evt_{microseconds_since_epoch}_{4_random_hex_chars}
    Sortable: lexicographic order matches chronological order.
    """
    ts_us = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"evt_{ts_us}_{rand}"




from kernos.utils import _safe_name
from kernos.utils import utc_now


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """An immutable record of something that happened. Never modified after creation."""

    id: str              # Unique, sortable: "evt_{ts_us}_{rand4}"
    type: str            # Hierarchical: "message.received", "tool.called", etc.
    tenant_id: str       # Isolation — always present
    timestamp: str       # ISO 8601 UTC
    source: str          # Emitting component: "handler", "capability_manager", "app"
    payload: dict        # Type-specific data
    metadata: dict       # Cross-cutting context: conversation_id, platform, etc.


# ---------------------------------------------------------------------------
# EventStream interface
# ---------------------------------------------------------------------------


class EventStream(ABC):
    """The kernel's nervous system. Append-only, multi-reader event log."""

    @abstractmethod
    async def emit(self, event: Event) -> None:
        """Write an event to the stream. Immutable once written."""
        ...

    @abstractmethod
    async def query(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Query events for a tenant, filtered by type and time range.

        Returns events in chronological order.
        NOT used for runtime context assembly — that's the State Store's job.
        """
        ...

    @abstractmethod
    async def count(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> int:
        """Count events matching filters. For dashboards and monitoring."""
        ...


# ---------------------------------------------------------------------------
# JSON file implementation
# ---------------------------------------------------------------------------


class JsonEventStream(EventStream):
    """JSON-on-disk event stream, partitioned by tenant and date.

    Path: {data_dir}/{tenant_id}/events/{date}.json
    Each file is a JSON array of event dicts, append-only.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _event_path(self, tenant_id: str) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return (
            self._data_dir / _safe_name(tenant_id) / "events" / f"{date_str}.json"
        )

    def _all_event_paths(self, tenant_id: str) -> list[Path]:
        events_dir = self._data_dir / _safe_name(tenant_id) / "events"
        if not events_dir.exists():
            return []
        return sorted(events_dir.glob("*.json"))

    async def emit(self, event: Event) -> None:
        path = self._event_path(event.tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    events = json.load(f)
            else:
                events = []
            events.append(asdict(event))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=2)

    async def query(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        all_events: list[Event] = []
        for path in self._all_event_paths(tenant_id):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_events = json.load(f)
                for e in raw_events:
                    if event_types and e["type"] not in event_types:
                        continue
                    if after and e["timestamp"] <= after:
                        continue
                    if before and e["timestamp"] >= before:
                        continue
                    all_events.append(Event(**e))
            except Exception as exc:
                logger.warning("Failed to read event file %s: %s", path, exc)

        # Already chronological (files sorted by date, events appended in order)
        return all_events[-limit:] if len(all_events) > limit else all_events

    async def count(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> int:
        results = await self.query(
            tenant_id, event_types=event_types, after=after, before=before, limit=100_000
        )
        return len(results)


# ---------------------------------------------------------------------------
# emit_event helper
# ---------------------------------------------------------------------------


async def emit_event(
    stream: EventStream,
    event_type: str,
    tenant_id: str,
    source: str,
    payload: dict,
    metadata: dict | None = None,
) -> Event:
    """Construct and emit an event. Returns the event (with its generated ID)."""
    event = Event(
        id=generate_event_id(),
        type=event_type,
        tenant_id=tenant_id,
        timestamp=utc_now(),
        source=source,
        payload=payload,
        metadata=metadata or {},
    )
    await stream.emit(event)
    return event
