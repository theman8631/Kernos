"""Workflow execution engine — runs workflows in the background.

WORKFLOW-LOOP-PRIMITIVE C5.

Triggers fire via ``TriggerRegistry``'s match listener; the engine
attaches one and translates each (Trigger, Event) into a
``WorkflowExecution`` record persisted to SQLite and enqueued on an
in-process asyncio queue. A background task drains the queue,
executing one workflow at a time. The workflow runs as an
ACTION-LOOP-PRIMITIVE shape: intent (trigger event payload), gather
(active spaces + synthetic context), action (run the action sequence
via the action library), verify (per-step verifier + workflow-level
verifier), decide (complete / abort / retry).

Synthetic CohortContext-equivalent (Kit edit, narrow review): the
context constructed at execution start matches the shipped
``CohortContext`` shape — ``instance_id``, ``member_id``,
``user_message`` (synthetic placeholder describing the trigger
event), ``conversation_thread`` (empty tuple), ``active_spaces``
(resolved by the engine's space resolver), ``turn_id`` (synthetic
``"workflow:"`` + execution_id), ``produced_at``. Kick-back
trigger fires if active-space resolution fails for an instance —
the engine emits a kickback event and aborts the execution rather
than running covenant-blind.

Approval gates: when an action descriptor references a gate, the
engine first **executes the action**, then **pauses AFTER** waiting
for an approval event matching the gate's predicate. Per the spec's
"action first → pause AFTER → wait → resume" semantics. Timeout
behaviour is set per gate descriptor:

  - ``abort_workflow``: emit terminated(reason=gate_timeout); end.
  - ``escalate_to_owner``: emit owner_escalation event; abort.
  - ``auto_proceed_with_default``: continue with the gate's
    default_value; safe-deny enforcement at workflow registration
    prevents any irreversible downstream action.

Restart-resume: ``workflow_executions`` SQLite table records the
state of every execution. On engine start, executions in
``running`` state are inspected; if the next-to-run action is
``resume_safe``, the execution is re-enqueued at that step;
otherwise it's aborted with ``aborted_by_restart``. Default
``resume_safe = False`` — conservative.

Audit events emitted to event_stream:

  - ``workflow.execution_started``
  - ``workflow.execution_step_succeeded``
  - ``workflow.execution_step_failed``
  - ``workflow.execution_paused`` (entered approval gate)
  - ``workflow.execution_resumed`` (gate satisfied)
  - ``workflow.execution_terminated``

All carry the execution's ``correlation_id`` so audit chains compose
with the rest of Kernos's event taxonomy.
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

from kernos.kernel import event_stream
from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    ContextSpaceRef,
)
from kernos.kernel.event_stream import Event
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.predicates import evaluate as evaluate_predicate
from kernos.kernel.workflows.trigger_registry import Trigger, TriggerRegistry
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Workflow,
    WorkflowRegistry,
)

logger = logging.getLogger(__name__)


# Active-space resolver. The engine calls this to populate the
# synthetic CohortContext.active_spaces tuple. Real implementations
# read ContextSpace by instance_id; tests inject a stub.
ActiveSpaceResolver = Callable[[str], Awaitable[tuple[ContextSpaceRef, ...]]]


# ---------------------------------------------------------------------------
# WorkflowExecution record
# ---------------------------------------------------------------------------


@dataclass
class WorkflowExecution:
    execution_id: str
    workflow_id: str
    instance_id: str
    correlation_id: str
    state: str  # queued | running | completed | aborted
    action_index_completed: int = -1
    intermediate_state: dict = field(default_factory=dict)
    last_heartbeat: str = ""
    aborted_reason: str = ""
    started_at: str = ""
    terminated_at: str = ""
    trigger_event_payload: dict = field(default_factory=dict)
    trigger_event_id: str = ""
    member_id: str = ""

    def to_row(self) -> tuple:
        return (
            self.execution_id, self.workflow_id, self.instance_id,
            self.correlation_id, self.state, self.action_index_completed,
            json.dumps(self.intermediate_state), self.last_heartbeat,
            self.aborted_reason, self.started_at, self.terminated_at,
            json.dumps(self.trigger_event_payload), self.trigger_event_id,
            self.member_id,
        )

    @classmethod
    def from_row(cls, row) -> "WorkflowExecution":
        try:
            intermediate = json.loads(row["intermediate_state"]) or {}
        except Exception:
            intermediate = {}
        try:
            payload = json.loads(row["trigger_event_payload"]) or {}
        except Exception:
            payload = {}
        return cls(
            execution_id=row["execution_id"],
            workflow_id=row["workflow_id"],
            instance_id=row["instance_id"],
            correlation_id=row["correlation_id"],
            state=row["state"],
            action_index_completed=row["action_index_completed"],
            intermediate_state=intermediate,
            last_heartbeat=row["last_heartbeat"] or "",
            aborted_reason=row["aborted_reason"] or "",
            started_at=row["started_at"] or "",
            terminated_at=row["terminated_at"] or "",
            trigger_event_payload=payload,
            trigger_event_id=row["trigger_event_id"] or "",
            member_id=row["member_id"] or "",
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_executions (
    execution_id            TEXT PRIMARY KEY,
    workflow_id             TEXT NOT NULL,
    instance_id             TEXT NOT NULL,
    correlation_id          TEXT NOT NULL,
    state                   TEXT NOT NULL,
    action_index_completed  INTEGER DEFAULT -1,
    intermediate_state      TEXT DEFAULT '{}',
    last_heartbeat          TEXT DEFAULT '',
    aborted_reason          TEXT DEFAULT '',
    started_at              TEXT NOT NULL,
    terminated_at           TEXT DEFAULT '',
    trigger_event_payload   TEXT DEFAULT '{}',
    trigger_event_id        TEXT DEFAULT '',
    member_id               TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_executions_state
    ON workflow_executions(instance_id, state);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _EXECUTIONS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionEngine:
    """Background workflow execution. One engine per Kernos
    installation; one queue; one worker task; sequential dispatch."""

    def __init__(self) -> None:
        self._trigger_registry: TriggerRegistry | None = None
        self._workflow_registry: WorkflowRegistry | None = None
        self._action_library: ActionLibrary | None = None
        self._ledger: WorkflowLedger | None = None
        self._space_resolver: ActiveSpaceResolver | None = None
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._queue: asyncio.Queue[WorkflowExecution] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._listener_callable: Callable | None = None
        self._gate_waiters: dict[str, asyncio.Event] = {}
        # gate_waiters: execution_id → Event signalled when an
        # approval event matching the current gate predicate flushes.
        self._gate_predicates: dict[str, dict] = {}
        # gate_predicates: execution_id → current gate's
        # approval_event_predicate.
        self._gate_event_types: dict[str, str] = {}
        self._gate_hook_registered: bool = False

    # -- lifecycle ------------------------------------------------------

    async def start(
        self,
        data_dir: str,
        trigger_registry: TriggerRegistry,
        workflow_registry: WorkflowRegistry,
        action_library: ActionLibrary,
        ledger: WorkflowLedger,
        *,
        space_resolver: ActiveSpaceResolver | None = None,
    ) -> None:
        if self._db is not None:
            return
        self._trigger_registry = trigger_registry
        self._workflow_registry = workflow_registry
        self._action_library = action_library
        self._ledger = ledger
        self._space_resolver = space_resolver
        self._db_path = Path(data_dir) / "instance.db"
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)
        self._stop_event = asyncio.Event()
        # Register the trigger match listener.
        self._listener_callable = self._on_trigger_match
        trigger_registry.add_match_listener(self._listener_callable)
        # Register the approval-gate post-flush hook.
        if not self._gate_hook_registered:
            event_stream.register_post_flush_hook(self._on_post_flush_for_gates)
            self._gate_hook_registered = True
        # Restart-resume: re-enqueue running executions where the next
        # action is resume-safe; abort the rest with aborted_by_restart.
        await self._restart_resume_pass()
        self._worker_task = asyncio.create_task(
            self._worker(), name="workflow_execution_engine",
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._worker_task is not None:
            try:
                # Drain by enqueueing a sentinel so the worker can wake.
                self._queue.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(self._worker_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._worker_task = None
        if self._listener_callable is not None and self._trigger_registry is not None:
            self._trigger_registry.remove_match_listener(self._listener_callable)
            self._listener_callable = None
        if self._gate_hook_registered:
            event_stream.unregister_post_flush_hook(self._on_post_flush_for_gates)
            self._gate_hook_registered = False
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- trigger match → enqueue ---------------------------------------

    async def _on_trigger_match(self, trigger: Trigger, event: Event) -> None:
        """TriggerRegistry calls this when a trigger matches a durable
        event. Persist a queued WorkflowExecution and push it on the
        engine queue."""
        if self._db is None:
            return
        execution = WorkflowExecution(
            execution_id=str(uuid.uuid4()),
            workflow_id=trigger.workflow_id,
            instance_id=event.instance_id,
            correlation_id=str(uuid.uuid4()),
            state="queued",
            started_at=_now(),
            trigger_event_payload=event.payload,
            trigger_event_id=event.event_id,
            member_id=event.member_id or "",
        )
        await self._db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        self._queue.put_nowait(execution)

    # -- worker ---------------------------------------------------------

    async def _worker(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                execution = await asyncio.wait_for(
                    self._queue.get(), timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue
            if execution is None:
                # Sentinel — stop drain.
                break
            try:
                await self._run_execution(execution)
            except Exception as exc:
                logger.warning(
                    "WORKFLOW_EXECUTION_FAILED execution_id=%s error=%s",
                    execution.execution_id, exc, exc_info=True,
                )

    # -- execution ------------------------------------------------------

    async def _run_execution(self, execution: WorkflowExecution) -> None:
        assert self._workflow_registry is not None
        assert self._action_library is not None
        # Mark running.
        await self._update_state(execution, "running")
        await event_stream.emit(
            execution.instance_id, "workflow.execution_started",
            {"workflow_id": execution.workflow_id,
             "execution_id": execution.execution_id,
             "trigger_event_id": execution.trigger_event_id},
            correlation_id=execution.correlation_id,
            member_id=execution.member_id or None,
        )
        wf = await self._workflow_registry.get_workflow(execution.workflow_id)
        if wf is None:
            await self._abort(execution, "workflow_not_found")
            return
        # Bounds enforcement (Codex review post-C7): wrap the rest of
        # the run in asyncio.wait_for so wall_time_seconds bounds are
        # actually enforced at runtime — registration requires the
        # field, so the runtime should honour it. iteration_count and
        # cost_usd bounds are not yet enforceable for sequential
        # action chains; future work.
        wall_time = wf.bounds.wall_time_seconds
        if wall_time is not None and wall_time > 0:
            try:
                await asyncio.wait_for(
                    self._run_action_sequence(execution, wf),
                    timeout=wall_time,
                )
            except asyncio.TimeoutError:
                await self._abort(execution, "wall_time_exceeded")
            return
        await self._run_action_sequence(execution, wf)

    async def _run_action_sequence(
        self, execution: WorkflowExecution, wf: Workflow,
    ) -> None:
        # Build synthetic CohortContext.
        try:
            context = await self._build_context(execution, wf)
        except _ContextBuildError as exc:
            await self._abort(execution, f"context_build_failed:{exc}")
            return
        gate_by_name = {g.gate_name: g for g in wf.approval_gates}
        start_idx = max(0, execution.action_index_completed + 1)
        for idx in range(start_idx, len(wf.action_sequence)):
            action = wf.action_sequence[idx]
            verb = self._action_library.get(action.action_type)
            try:
                result = await verb.execute(context, action.parameters)
            except Exception as exc:
                await self._record_step_failed(
                    execution, idx, action, error=f"execute_raised:{exc}",
                )
                await self._abort(execution, f"step_{idx}_raised")
                return
            verified = False
            try:
                verified = await verb.verify(context, action.parameters, result)
            except Exception as exc:
                logger.warning(
                    "VERIFY_RAISED execution_id=%s step=%s error=%s",
                    execution.execution_id, idx, exc,
                )
            if not result.success or not verified:
                await self._record_step_failed(
                    execution, idx, action,
                    error=result.error or "verifier_rejected",
                )
                if action.continuation_rules.on_failure == "abort":
                    await self._abort(execution, f"step_{idx}_failed")
                    return
                # continue/retry: v1 just continues; retry budget is
                # observed by the action library's own dispatch path
                # in future iterations.
            else:
                await self._record_step_succeeded(execution, idx, action, result)
            # Approval-gate handling: action FIRST, pause AFTER.
            if action.gate_ref is not None:
                gate = gate_by_name[action.gate_ref]
                cont = await self._await_gate(execution, gate)
                if not cont:
                    return  # _await_gate aborted
            await self._mark_step_complete(execution, idx)
        # All steps done — mark completed.
        await self._complete(execution)

    async def _await_gate(
        self, execution: WorkflowExecution, gate: ApprovalGate,
    ) -> bool:
        """Pause until an approval event matches the gate predicate, or
        timeout. Returns True if the engine should continue with the
        next action, False if it aborted the execution."""
        await event_stream.emit(
            execution.instance_id, "workflow.execution_paused",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name,
             "approval_event_type": gate.approval_event_type},
            correlation_id=execution.correlation_id,
        )
        ev = asyncio.Event()
        self._gate_waiters[execution.execution_id] = ev
        self._gate_predicates[execution.execution_id] = gate.approval_event_predicate
        self._gate_event_types[execution.execution_id] = gate.approval_event_type
        try:
            await asyncio.wait_for(ev.wait(), timeout=gate.timeout_seconds)
        except asyncio.TimeoutError:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
            return await self._handle_gate_timeout(execution, gate)
        finally:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
        await event_stream.emit(
            execution.instance_id, "workflow.execution_resumed",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name},
            correlation_id=execution.correlation_id,
        )
        return True

    async def _handle_gate_timeout(
        self, execution: WorkflowExecution, gate: ApprovalGate,
    ) -> bool:
        if gate.bound_behavior_on_timeout == "abort_workflow":
            await self._abort(execution, f"gate_timeout:{gate.gate_name}")
            return False
        if gate.bound_behavior_on_timeout == "escalate_to_owner":
            await event_stream.emit(
                execution.instance_id, "workflow.owner_escalation",
                {"execution_id": execution.execution_id,
                 "gate_name": gate.gate_name},
                correlation_id=execution.correlation_id,
            )
            await self._abort(execution, f"gate_escalated:{gate.gate_name}")
            return False
        # auto_proceed_with_default
        await event_stream.emit(
            execution.instance_id, "workflow.gate_auto_proceeded",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name,
             "default_value": gate.default_value},
            correlation_id=execution.correlation_id,
        )
        return True

    async def _on_post_flush_for_gates(self, batch: list[Event]) -> None:
        """Post-flush hook that resolves approval-gate waits.

        Runs in addition to the trigger registry's hook. For each
        active gate-wait, scan the batch for events that match the
        gate's approval_event_type + approval_event_predicate. On
        match, set the corresponding asyncio.Event."""
        if not self._gate_waiters:
            return
        for execution_id, waiter in list(self._gate_waiters.items()):
            event_type = self._gate_event_types.get(execution_id)
            predicate = self._gate_predicates.get(execution_id)
            if event_type is None or predicate is None:
                continue
            for event in batch:
                if event.event_type != event_type:
                    continue
                try:
                    if evaluate_predicate(predicate, event):
                        waiter.set()
                        break
                except Exception:
                    pass

    # -- restart-resume -------------------------------------------------

    async def _restart_resume_pass(self) -> None:
        assert self._db is not None
        assert self._workflow_registry is not None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE state = 'running'"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            execution = WorkflowExecution.from_row(row)
            wf = await self._workflow_registry.get_workflow(execution.workflow_id)
            next_idx = execution.action_index_completed + 1
            if (
                wf is not None
                and 0 <= next_idx < len(wf.action_sequence)
                and wf.action_sequence[next_idx].resume_safe
            ):
                await event_stream.emit(
                    execution.instance_id, "workflow.execution_resumed",
                    {"execution_id": execution.execution_id,
                     "reason": "restart_resume",
                     "from_step": next_idx},
                    correlation_id=execution.correlation_id,
                )
                self._queue.put_nowait(execution)
            else:
                # Conservative default: not resume-safe → abort.
                await self._abort(execution, "aborted_by_restart")

    # -- audit + persistence helpers -----------------------------------

    async def _update_state(
        self, execution: WorkflowExecution, state: str,
    ) -> None:
        assert self._db is not None
        execution.state = state
        execution.last_heartbeat = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (state, execution.last_heartbeat, execution.execution_id),
        )

    async def _mark_step_complete(
        self, execution: WorkflowExecution, idx: int,
    ) -> None:
        assert self._db is not None
        execution.action_index_completed = idx
        await self._db.execute(
            "UPDATE workflow_executions SET action_index_completed = ?, "
            "last_heartbeat = ? WHERE execution_id = ?",
            (idx, _now(), execution.execution_id),
        )

    async def _record_step_succeeded(
        self,
        execution: WorkflowExecution,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
    ) -> None:
        await event_stream.emit(
            execution.instance_id, "workflow.execution_step_succeeded",
            {"execution_id": execution.execution_id,
             "step_index": idx,
             "action_type": action.action_type},
            correlation_id=execution.correlation_id,
        )
        if self._ledger is not None:
            try:
                await self._ledger.append(
                    execution.instance_id, execution.workflow_id,
                    {"execution_id": execution.execution_id,
                     "step_index": idx,
                     "agent_or_action": action.action_type,
                     "synopsis": f"{action.action_type} succeeded",
                     "result_summary": "success",
                     "kickback_if_any": ""},
                )
            except Exception as exc:
                logger.warning("LEDGER_APPEND_FAILED %s", exc)

    async def _record_step_failed(
        self,
        execution: WorkflowExecution,
        idx: int,
        action: ActionDescriptor,
        *,
        error: str,
    ) -> None:
        await event_stream.emit(
            execution.instance_id, "workflow.execution_step_failed",
            {"execution_id": execution.execution_id,
             "step_index": idx,
             "action_type": action.action_type,
             "error": error},
            correlation_id=execution.correlation_id,
        )
        if self._ledger is not None:
            try:
                await self._ledger.append(
                    execution.instance_id, execution.workflow_id,
                    {"execution_id": execution.execution_id,
                     "step_index": idx,
                     "agent_or_action": action.action_type,
                     "synopsis": f"{action.action_type} failed",
                     "result_summary": "failed",
                     "kickback_if_any": error},
                )
            except Exception as exc:
                logger.warning("LEDGER_APPEND_FAILED %s", exc)

    async def _abort(
        self, execution: WorkflowExecution, reason: str,
    ) -> None:
        assert self._db is not None
        execution.state = "aborted"
        execution.aborted_reason = reason
        execution.terminated_at = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, aborted_reason = ?, "
            "terminated_at = ? WHERE execution_id = ?",
            ("aborted", reason, execution.terminated_at, execution.execution_id),
        )
        await event_stream.emit(
            execution.instance_id, "workflow.execution_terminated",
            {"execution_id": execution.execution_id,
             "workflow_id": execution.workflow_id,
             "outcome": "aborted",
             "reason": reason},
            correlation_id=execution.correlation_id,
        )

    async def _complete(self, execution: WorkflowExecution) -> None:
        assert self._db is not None
        execution.state = "completed"
        execution.terminated_at = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, terminated_at = ? "
            "WHERE execution_id = ?",
            ("completed", execution.terminated_at, execution.execution_id),
        )
        await event_stream.emit(
            execution.instance_id, "workflow.execution_terminated",
            {"execution_id": execution.execution_id,
             "workflow_id": execution.workflow_id,
             "outcome": "completed"},
            correlation_id=execution.correlation_id,
        )

    # -- context construction ------------------------------------------

    async def _build_context(
        self, execution: WorkflowExecution, wf: Workflow,
    ) -> CohortContext:
        if self._space_resolver is not None:
            try:
                spaces = await self._space_resolver(execution.instance_id)
            except Exception as exc:
                raise _ContextBuildError(
                    f"active_space_resolution_failed: {exc}"
                ) from exc
        else:
            spaces = ()
        return CohortContext(
            member_id=execution.member_id or "workflow",
            user_message=(
                f"workflow:{wf.workflow_id} fired by trigger event "
                f"{execution.trigger_event_id}"
            ),
            conversation_thread=(),
            active_spaces=spaces,
            turn_id=f"workflow:{execution.execution_id}",
            instance_id=execution.instance_id,
            produced_at=execution.started_at,
        )

    # -- queries --------------------------------------------------------

    async def get_execution(
        self, execution_id: str,
    ) -> WorkflowExecution | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ) as cur:
            row = await cur.fetchone()
        return WorkflowExecution.from_row(row) if row else None

    async def list_executions(
        self, instance_id: str, *, state: str | None = None,
    ) -> list[WorkflowExecution]:
        if self._db is None:
            return []
        if state is None:
            query = (
                "SELECT * FROM workflow_executions WHERE instance_id = ? "
                "ORDER BY started_at"
            )
            args: tuple = (instance_id,)
        else:
            query = (
                "SELECT * FROM workflow_executions WHERE instance_id = ? "
                "AND state = ? ORDER BY started_at"
            )
            args = (instance_id, state)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [WorkflowExecution.from_row(r) for r in rows]


class _ContextBuildError(RuntimeError):
    """Internal: signal that synthetic CohortContext construction
    failed and the execution should be aborted."""


__all__ = [
    "ActiveSpaceResolver",
    "ExecutionEngine",
    "WorkflowExecution",
]
