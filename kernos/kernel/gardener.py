"""GardenerService — per-instance canvas shape authority.

Runs the Gardener cohort against canvas events. Pillar 2 scaffolding
(state + event subscription + public API surface). Pillars 3 and 4 fill
in the initial-shape and continuous-evolution logic.

Design notes:
  - One GardenerService per Kernos instance, held by MessageHandler.
  - Subscribes to canvas events via ``on_canvas_event(iid, etype,
    payload, *, member_id="")`` — the same signature as the existing
    handler-level ``_canvas_emit`` callback, so wiring fans the event
    out to the Gardener after emission to the unified event stream.
  - Ignores events it emitted itself (``source: gardener`` in payload)
    to prevent cascading reshapes (spec Hazard B).
  - Pattern content for initial-shape + evolution consultations is
    loaded from the Workflow Patterns canvas lazily and cached with
    mtime-style invalidation on Workflow Patterns canvas changes
    (spec Hazard A).
  - Proposals coalesce within a per-canvas 24-hour window; see
    :class:`ProposalCoalescer`.

Non-blocking discipline: every handler entry point schedules work with
``asyncio.create_task`` so member-facing turns never block on a
Gardener consultation. Errors are logged and swallowed — canvas ops
must never break because the Gardener had a bad turn.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from kernos.cohorts.gardener import (
    EvolutionContext,
    GardenerDecision,
    GardenerExhausted,
    InitialShapeContext,
    SectionContext,
    judge_evolution,
    judge_initial_shape,
    judge_section_management,
)

logger = logging.getLogger(__name__)


#: Default coalescing window for evolution proposals (spec Pillar 4).
DEFAULT_COALESCE_MINUTES = 24 * 60  # 24 hours

#: Sentinel used in emitted events so the Gardener skips events it caused.
GARDENER_SOURCE = "gardener"

#: Name of the team canvas that stores the judgment-input library.
WORKFLOW_PATTERNS_CANVAS_NAME = "Workflow Patterns"


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


@dataclass
class PendingProposal:
    canvas_id: str
    action: str
    confidence: str
    rationale: str
    affected_pages: list[str]
    captured_at: datetime
    payload: dict = field(default_factory=dict)


class ProposalCoalescer:
    """Per-canvas rolling window of high-confidence proposals.

    In-process only. Restart drops the buffer — acceptable for v1; a
    persistent variant ships as a follow-on if the post-v1 observation
    shows rapid restart cycles losing noticeable work.
    """

    def __init__(self, window: timedelta = timedelta(minutes=DEFAULT_COALESCE_MINUTES)) -> None:
        self._window = window
        self._buffers: dict[str, list[PendingProposal]] = {}
        self._last_surface: dict[str, datetime] = {}

    @property
    def window(self) -> timedelta:
        return self._window

    def add(self, proposal: PendingProposal) -> None:
        self._buffers.setdefault(proposal.canvas_id, []).append(proposal)

    def should_surface(self, canvas_id: str, now: datetime | None = None) -> bool:
        """True if the window has elapsed since the last surface for this canvas."""
        now = now or datetime.now(timezone.utc)
        last = self._last_surface.get(canvas_id)
        if last is None:
            return True
        return (now - last) >= self._window

    def drain(self, canvas_id: str, now: datetime | None = None) -> list[PendingProposal]:
        """Return pending proposals for a canvas and mark window start."""
        now = now or datetime.now(timezone.utc)
        buf = self._buffers.pop(canvas_id, [])
        if buf:
            self._last_surface[canvas_id] = now
        return buf

    def buffered_count(self, canvas_id: str) -> int:
        return len(self._buffers.get(canvas_id, []))


# ---------------------------------------------------------------------------
# Pattern cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedPattern:
    name: str
    body: str
    frontmatter: dict


class PatternCache:
    """In-memory cache of Workflow Patterns canvas pages.

    Populated lazily on first consultation and invalidated on canvas
    events whose canvas_id matches the Workflow Patterns canvas
    (spec Hazard A).
    """

    def __init__(self) -> None:
        self._patterns: dict[str, _CachedPattern] = {}
        self._loaded = False
        self._workflow_canvas_id: str = ""

    def set_workflow_canvas_id(self, canvas_id: str) -> None:
        self._workflow_canvas_id = canvas_id

    def invalidate_if_workflow(self, canvas_id: str) -> bool:
        if canvas_id and canvas_id == self._workflow_canvas_id:
            self._patterns.clear()
            self._loaded = False
            return True
        return False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def put(self, name: str, body: str, frontmatter: dict | None = None) -> None:
        self._patterns[name] = _CachedPattern(
            name=name, body=body, frontmatter=dict(frontmatter or {}),
        )

    def get(self, name: str) -> _CachedPattern | None:
        return self._patterns.get(name)

    def all_summaries(self) -> list[dict]:
        """Structured pattern catalog for the initial-shape prompt."""
        return [
            {
                "name": p.name,
                "summary": _short_summary(p.body),
                "frontmatter": p.frontmatter,
            }
            for p in self._patterns.values()
            if p.name not in ("library-meta", "")
        ]

    def mark_loaded(self) -> None:
        self._loaded = True

    def cross_pattern_body(self) -> str:
        meta = self._patterns.get("library-meta")
        return meta.body if meta else ""


def _short_summary(body: str, max_chars: int = 600) -> str:
    """Cheap extractive summary: first paragraph after the heading."""
    lines = [l for l in body.splitlines() if l.strip()]
    out: list[str] = []
    for line in lines:
        if line.startswith("#"):
            continue
        out.append(line)
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n".join(out)[:max_chars]


# ---------------------------------------------------------------------------
# GardenerService
# ---------------------------------------------------------------------------


class GardenerService:
    """Per-instance canvas shape authority. See module docstring."""

    def __init__(
        self,
        *,
        canvas_service: Any,
        instance_db: Any,
        reasoning_service: Any,
        coalesce_window: timedelta | None = None,
        event_emit: Callable[..., Any] | None = None,
    ) -> None:
        self._canvas = canvas_service
        self._db = instance_db
        self._reasoning = reasoning_service
        self._coalescer = ProposalCoalescer(
            window=coalesce_window or timedelta(minutes=DEFAULT_COALESCE_MINUTES),
        )
        self._patterns = PatternCache()
        #: Event-emit callback for Gardener-produced events
        #: (``canvas.reshaped``, ``gardener.proposal_batch``). Same signature
        #: as CanvasService's event_emit.
        self._event_emit = event_emit
        #: Track in-flight consultation tasks so tests can await them.
        self._in_flight: set[asyncio.Task] = set()

    # ---- Public API ------------------------------------------------------

    async def on_canvas_event(
        self, instance_id: str, event_type: str, payload: dict,
        *, member_id: str = "",
    ) -> None:
        """Event-stream consumer. Non-blocking; schedules background work.

        Swallows everything. The canvas pipeline called us; we mustn't
        propagate errors back into it.
        """
        try:
            if not isinstance(payload, dict):
                return
            # Ignore our own emissions (prevents cascades — spec Hazard B).
            if (payload.get("source") or "").lower() == GARDENER_SOURCE:
                return
            # Invalidate pattern cache if the Workflow Patterns canvas changed.
            cid = payload.get("canvas_id", "")
            if cid and self._patterns.invalidate_if_workflow(cid):
                logger.info("GARDENER_PATTERN_CACHE_INVALIDATED: canvas=%s", cid)

            # Schedule background dispatch — never block the caller.
            task = asyncio.create_task(
                self._dispatch(instance_id, event_type, payload),
                name=f"gardener_dispatch_{event_type}",
            )
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_ON_EVENT_FAILED: %s", exc)

    async def consult_initial_shape(
        self, ctx: InitialShapeContext,
    ) -> GardenerDecision | None:
        """Pillar 3 entry — pick a pattern for a new canvas."""
        await self._ensure_patterns_loaded(ctx.instance_id)
        ctx.available_patterns = self._patterns.all_summaries()
        try:
            return await judge_initial_shape(
                ctx, reasoning_service=self._reasoning,
            )
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_INITIAL_SHAPE: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_INITIAL_SHAPE_FAILED: %s", exc)
            return None

    async def consult_evolution(
        self, ctx: EvolutionContext,
    ) -> GardenerDecision | None:
        """Pillar 4 entry — run evolution heuristics on a canvas event."""
        await self._ensure_patterns_loaded(ctx.instance_id)
        ctx.cross_pattern_heuristics = self._patterns.cross_pattern_body()
        try:
            return await judge_evolution(ctx, reasoning_service=self._reasoning)
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_EVOLUTION: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_EVOLUTION_FAILED: %s", exc)
            return None

    async def consult_section(
        self, ctx: SectionContext,
    ) -> GardenerDecision | None:
        """Pillar 4 sub-entry — section-management judgment."""
        try:
            return await judge_section_management(
                ctx, reasoning_service=self._reasoning,
            )
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_SECTION: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_SECTION_FAILED: %s", exc)
            return None

    @property
    def coalescer(self) -> ProposalCoalescer:
        return self._coalescer

    @property
    def patterns(self) -> PatternCache:
        return self._patterns

    async def wait_idle(self) -> None:
        """Test helper — wait for all in-flight dispatch tasks to complete."""
        while self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    # ---- Internal --------------------------------------------------------

    async def _dispatch(
        self, instance_id: str, event_type: str, payload: dict,
    ) -> None:
        """Route an event to the right judgment surface. Pillar 4 wires this."""
        # Pillar 2 scaffolding — concrete routing lands with Pillars 3 and 4.
        # This method exists so the event-subscription path is live; it
        # just logs for now and is overridden at wiring time.
        logger.debug(
            "GARDENER_DISPATCH: instance=%s event=%s payload_keys=%s",
            instance_id, event_type, sorted((payload or {}).keys()),
        )

    async def _ensure_patterns_loaded(self, instance_id: str) -> None:
        """Lazy-load the Workflow Patterns canvas into the pattern cache."""
        if self._patterns.loaded:
            return
        if self._db is None or self._canvas is None:
            return
        try:
            row = await self._db.find_canvas_by_name(
                name=WORKFLOW_PATTERNS_CANVAS_NAME, scope="team",
            )
            if not row:
                logger.info(
                    "GARDENER_PATTERNS_MISSING: Workflow Patterns canvas not seeded",
                )
                self._patterns.mark_loaded()  # avoid hot-looping the lookup
                return
            canvas_id = row["canvas_id"]
            self._patterns.set_workflow_canvas_id(canvas_id)

            pages = await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            for page in pages:
                path = page.get("path", "")
                if not path or path == "index.md":
                    continue
                pr = await self._canvas.page_read(
                    instance_id=instance_id, canvas_id=canvas_id,
                    page_slug=path,
                )
                if not pr.ok:
                    continue
                fm = pr.extra.get("frontmatter", {}) or {}
                body = pr.extra.get("body", "") or ""
                name = fm.get("pattern") or path.rsplit(".", 1)[0]
                self._patterns.put(name, body, fm)
            self._patterns.mark_loaded()
            logger.info(
                "GARDENER_PATTERNS_LOADED: count=%d", len(self._patterns._patterns),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_PATTERN_LOAD_FAILED: %s", exc)
            self._patterns.mark_loaded()  # don't thrash the cache on repeated failures

    async def _emit(
        self, instance_id: str, event_type: str, payload: dict,
        *, member_id: str = "",
    ) -> None:
        """Emit an event tagged ``source: gardener`` so we skip our own events."""
        if self._event_emit is None:
            return
        try:
            tagged = dict(payload)
            tagged.setdefault("source", GARDENER_SOURCE)
            await self._event_emit(instance_id, event_type, tagged, member_id=member_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_EMIT_FAILED: %s %s", event_type, exc)
