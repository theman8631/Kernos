"""DrafterCohort — tool-starved system cohort (DRAFTER v2 spec).

C1 ships the cohort skeleton: lifecycle (``start``), emitter
registration, cursor initialization, port wiring. The full evaluation
pipeline (``tick``, two-tier wake/no-op, recognition, signals) lands in
C2 + C3.

Anti-fragmentation invariant: Drafter consumes shared context surfaces
(event_stream, friction observer signals, WDP DraftRegistry, STS query
surfaces, principal cohort context). Drafter does NOT build a parallel
cohort-specific context model. When the shared-situation-model spec
lands post-CRB, Drafter slots in by consuming the new shared object
without internal rework. Reviewers should reject changes that introduce
Drafter-private context state, parallel friction-detection logic, or
shadow registries.

Future-composition invariant: Drafter is the first tool-starved cohort.
The universal cohort substrate enforces tool restriction generically;
future Pattern Observer and Curator cohorts inherit the same pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts._substrate.cursor import DurableEventCursor
from kernos.kernel.cohorts._substrate.tool_restriction import (
    CohortToolWhitelist,
)
from kernos.kernel.cohorts.drafter.errors import DrafterToolForbidden
from kernos.kernel.cohorts.drafter.ports import (
    DRAFTER_WHITELIST,
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)


# ---------------------------------------------------------------------------
# Tick result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickResult:
    """Output of one ``DrafterCohort.tick()`` pass."""

    instance_id: str
    events_processed: int
    cursor_position_before: str
    cursor_position_after: str
    signals_emitted: tuple[str, ...] = ()
    receipts_emitted: int = 0
    budget_used: int = 0
    budget_remaining: int = 0
    no_op: bool = True


# ---------------------------------------------------------------------------
# DrafterCohort
# ---------------------------------------------------------------------------


class DrafterCohort:
    """Tool-starved system cohort observing conversation events; holds
    workflow drafts via WDP; validates via STS dry-run; signals principal
    when committable.

    Constructor takes restricted *ports* (Kit pin v1→v2), NOT raw
    ``DraftRegistry`` / ``SubstrateTools`` / ``EventEmitter`` objects.
    Forbidden capabilities (``mark_committed``, full ``register_workflow``,
    raw ``emit``) are STRUCTURALLY ABSENT from the port surface — the
    Python attribute lookup raises ``AttributeError`` before any
    whitelist check runs.

    The ``CohortToolWhitelist`` substrate is wired as belt-and-suspenders
    for any path that escapes the port surface (serialization, reflection).
    """

    def __init__(
        self,
        *,
        draft_port: DrafterDraftPort,
        substrate_tools_port: DrafterSubstrateToolsPort,
        event_port: DrafterEventPort,
        cursor: DurableEventCursor,
        action_log: ActionLog,
        # Budget + compiler_helper land in C2/C3; declared optional in C1
        # so the skeleton imports without forward-referenced dependencies.
        budget: Any | None = None,
        compiler_helper: Optional[Callable[[Any], dict]] = None,
        clock: Any | None = None,
        whitelist: CohortToolWhitelist | None = None,
    ) -> None:
        self._draft_port = draft_port
        self._substrate_tools_port = substrate_tools_port
        self._event_port = event_port
        self._cursor = cursor
        self._action_log = action_log
        self._budget = budget
        self._compiler_helper = compiler_helper
        self._clock = clock
        # Belt-and-suspenders whitelist. Construct a default one if the
        # caller didn't supply — the typical call site lets the cohort
        # build its own from DRAFTER_WHITELIST + DrafterToolForbidden.
        self._whitelist = whitelist or CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=DRAFTER_WHITELIST,
            forbidden_exception=DrafterToolForbidden,
        )
        self._started = False

    # -- properties -----------------------------------------------------

    @property
    def cohort_id(self) -> str:
        return "drafter"

    @property
    def cursor(self) -> DurableEventCursor:
        return self._cursor

    @property
    def action_log(self) -> ActionLog:
        return self._action_log

    @property
    def whitelist(self) -> CohortToolWhitelist:
        return self._whitelist

    @property
    def draft_port(self) -> DrafterDraftPort:
        return self._draft_port

    @property
    def substrate_tools_port(self) -> DrafterSubstrateToolsPort:
        return self._substrate_tools_port

    @property
    def event_port(self) -> DrafterEventPort:
        return self._event_port

    # -- lifecycle ------------------------------------------------------

    def start(self, *, instance_id: str) -> None:
        """Wire cohort at engine bring-up. Idempotent.

        Engine is responsible for:

        1. Registering the ``"drafter"`` source via
           :func:`kernos.kernel.event_stream.emitter_registry().register("drafter")`
           BEFORE constructing :class:`DrafterEventPort`. The port's
           constructor verifies the registered emitter's source identity.
        2. Starting :class:`CursorStore` and :class:`ActionLog` (their
           ``start(data_dir)`` methods open SQLite connections).
        3. Constructing :class:`DrafterCohort` with the wired ports
           and calling :meth:`start` per instance.
        """
        if not instance_id:
            raise ValueError("instance_id is required")
        # The cursor is per-instance; the cohort itself is single-instance
        # per engine. We do NOT support multi-instance cohort objects in
        # v1 — engine bring-up constructs a fresh cohort + cursor per
        # instance if needed.
        if self._cursor.instance_id != instance_id:
            raise ValueError(
                f"DrafterCohort.start instance_id={instance_id!r} does "
                f"not match cursor instance_id="
                f"{self._cursor.instance_id!r}"
            )
        self._started = True

    @property
    def is_started(self) -> bool:
        return self._started

    async def tick(self, *, instance_id: str) -> TickResult:
        """Single evaluation pass. Reads new events from cursor; processes
        each per the wake/no-op fast path (C2); emits signals + receipts
        as needed (C3). Returns :class:`TickResult` with cursor
        advancement, events_processed, signals_emitted, budget remaining.

        C1 ships a skeleton that reads events but does not yet
        evaluate. Returns no_op=True with cursor advanced past read
        events.
        """
        if not self._started:
            raise RuntimeError("DrafterCohort.start() must be called first")
        if instance_id != self._cursor.instance_id:
            raise ValueError(
                f"tick instance_id={instance_id!r} does not match "
                f"cursor instance_id={self._cursor.instance_id!r}"
            )
        position_before = await self._cursor.current_position()
        events = await self._cursor.read_next_batch(max_events=10)
        # C1 skeleton: advance cursor past read events without
        # evaluation. Tier 1/Tier 2 evaluation lands in C2; signal +
        # receipt emission in C3.
        events_processed = 0
        for event in events:
            await self._cursor.commit_position(
                event_id=event.event_id, timestamp=event.timestamp,
            )
            events_processed += 1
        position_after = await self._cursor.current_position()
        return TickResult(
            instance_id=instance_id,
            events_processed=events_processed,
            cursor_position_before=position_before,
            cursor_position_after=position_after,
            signals_emitted=(),
            receipts_emitted=0,
            budget_used=0,
            budget_remaining=0,
            no_op=True,
        )


__all__ = ["DrafterCohort", "TickResult"]
