"""Workflow registry — durable workflow descriptors + atomic registration.

WORKFLOW-LOOP-PRIMITIVE C3. The registry owns the ``workflows`` SQLite
table and the validation pipeline that turns a parsed descriptor file
into a durable Workflow + Trigger pair. Both rows are persisted in a
single SQLite transaction; any failure rolls back fully so registration
is atomic across the two tables.

Persistence shape: the dataclass-shaped Workflow descriptor is
serialised as JSON into ``workflows.descriptor_json``. The columns
that need indexed lookup (workflow_id, instance_id, name, owner,
version, status, created_at) are stored as separate columns; the rest
of the descriptor body lives in the JSON blob. This trades structured
SQL queries on action sequences for simplicity — workflows are
instantiated by id and the engine reads the whole descriptor anyway.

Validation rules enforced at registration time:

  * Workflow MUST declare ``bounds`` (per ACTION-LOOP-PRIMITIVE; an
    unbounded workflow is rejected loudly).
  * Workflow MUST declare ``verifier`` (intent-satisfaction check; an
    workflow without a verifier is rejected loudly).
  * Every ``gate_ref`` on an ActionDescriptor MUST resolve to a gate
    declared in the workflow's ``approval_gates`` list.
  * ApprovalGate with ``bound_behavior_on_timeout=auto_proceed_with_default``
    MUST declare ``default_value``.
  * Safe-deny: ApprovalGate with ``auto_proceed_with_default`` MUST NOT
    have an irreversible action between it and the next gate (or
    workflow end). Reversibility is looked up via
    ``action_classification.is_irreversible``.
  * Predicate AST MUST validate via the predicates module.

Atomicity: ``register_workflow`` opens an explicit BEGIN / COMMIT
transaction on the shared instance.db connection; if any step raises
the transaction rolls back so no Workflow or Trigger row remains.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from kernos.kernel.workflows.action_classification import (
    KNOWN_ACTION_TYPES,
    is_irreversible,
)
from kernos.kernel.workflows.predicates import validate as validate_predicate
from kernos.kernel.workflows.trigger_registry import Trigger, TriggerRegistry

logger = logging.getLogger(__name__)


VALID_GATE_TIMEOUT_BEHAVIORS = frozenset({
    "abort_workflow",
    "escalate_to_owner",
    "auto_proceed_with_default",
})

VALID_VERIFIER_FLAVORS = frozenset({
    "deterministic",
    "llm_judged",
    "human_in_the_loop",
})

VALID_CONTINUATION_ON_FAILURE = frozenset({"abort", "continue", "retry"})

VALID_WORKFLOW_STATUSES = frozenset({"active", "paused", "retired"})


class WorkflowError(ValueError):
    """Raised when a workflow descriptor fails validation."""


# ---------------------------------------------------------------------------
# Descriptor dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Bounds:
    """Per ACTION-LOOP-PRIMITIVE: explicit termination bounds. At least
    one of ``iteration_count`` or ``wall_time_seconds`` MUST be set.
    ``cost_usd`` and ``composite`` are optional refinements."""

    iteration_count: int | None = None
    wall_time_seconds: int | None = None
    cost_usd: float | None = None
    composite: str | None = None  # "any" | "all"

    def is_empty(self) -> bool:
        return (
            self.iteration_count is None
            and self.wall_time_seconds is None
            and self.cost_usd is None
        )


@dataclass
class Verifier:
    """Per ACTION-LOOP-PRIMITIVE: intent-satisfaction check that
    determines whether a workflow run satisfied its declared intent."""

    flavor: str  # deterministic | llm_judged | human_in_the_loop
    check: str  # identifier / prompt-template / queue depending on flavor


@dataclass
class ApprovalGate:
    """Named pause-point in an action sequence. Engine waits for an
    approval event matching the gate's predicate before proceeding."""

    gate_name: str
    pause_reason: str
    approval_event_type: str
    approval_event_predicate: dict
    timeout_seconds: int
    bound_behavior_on_timeout: str  # abort_workflow | escalate_to_owner | auto_proceed_with_default
    default_value: Any | None = None


@dataclass
class ContinuationRules:
    on_failure: str = "abort"  # abort | continue | retry
    max_retries: int = 0


@dataclass
class ActionDescriptor:
    """A single step in a workflow's action sequence."""

    action_type: str
    parameters: dict = field(default_factory=dict)
    per_action_expectation: str = ""
    continuation_rules: ContinuationRules = field(default_factory=ContinuationRules)
    gate_ref: str | None = None
    resume_safe: bool = False


@dataclass
class TriggerDescriptor:
    """Trigger fields embedded in a workflow descriptor. The registry
    converts this to a full Trigger row at registration time, after
    minting trigger_id and copying the workflow's instance_id."""

    event_type: str
    predicate: dict
    predicate_source: str = ""
    actor_filter: str | None = None
    correlation_filter: str | None = None
    idempotency_key_template: str | None = None
    description: str = ""


@dataclass
class Workflow:
    """A durable workflow descriptor."""

    workflow_id: str
    instance_id: str
    name: str
    description: str
    owner: str
    version: str
    bounds: Bounds
    verifier: Verifier
    action_sequence: list[ActionDescriptor]
    approval_gates: list[ApprovalGate] = field(default_factory=list)
    trigger: TriggerDescriptor | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = ""
    status: str = "active"
    instance_local: bool = False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_workflow(wf: Workflow) -> None:
    """Raise ``WorkflowError`` if the workflow violates any structural
    invariant. Pure function — no I/O, no LLM."""
    if not isinstance(wf, Workflow):
        raise WorkflowError("validate_workflow expects a Workflow instance")
    if not wf.workflow_id:
        raise WorkflowError("workflow_id is required")
    if not wf.instance_id:
        raise WorkflowError("instance_id is required")
    if not wf.name:
        raise WorkflowError("name is required")
    if not wf.version:
        raise WorkflowError("version is required")
    # Bounds + verifier are required at the structural-invariant level.
    if not isinstance(wf.bounds, Bounds) or wf.bounds.is_empty():
        raise WorkflowError(
            "bounds is required (declare iteration_count, wall_time_seconds, "
            "or cost_usd)"
        )
    if not isinstance(wf.verifier, Verifier):
        raise WorkflowError("verifier is required")
    if wf.verifier.flavor not in VALID_VERIFIER_FLAVORS:
        raise WorkflowError(
            f"verifier.flavor must be one of {sorted(VALID_VERIFIER_FLAVORS)}, "
            f"got {wf.verifier.flavor!r}"
        )
    if not wf.verifier.check:
        raise WorkflowError("verifier.check is required")
    if wf.status not in VALID_WORKFLOW_STATUSES:
        raise WorkflowError(
            f"status must be one of {sorted(VALID_WORKFLOW_STATUSES)}, "
            f"got {wf.status!r}"
        )
    # Action sequence + classification.
    if not wf.action_sequence:
        raise WorkflowError("action_sequence must contain at least one action")
    for idx, action in enumerate(wf.action_sequence):
        if action.action_type not in KNOWN_ACTION_TYPES:
            raise WorkflowError(
                f"action_sequence[{idx}].action_type {action.action_type!r} "
                f"is not a known verb"
            )
        if action.continuation_rules.on_failure not in VALID_CONTINUATION_ON_FAILURE:
            raise WorkflowError(
                f"action_sequence[{idx}].continuation_rules.on_failure invalid"
            )
    # Approval gates.
    declared_gate_names = {g.gate_name for g in wf.approval_gates}
    if len(declared_gate_names) != len(wf.approval_gates):
        raise WorkflowError("approval_gates contains duplicate gate_name entries")
    for gate in wf.approval_gates:
        if gate.bound_behavior_on_timeout not in VALID_GATE_TIMEOUT_BEHAVIORS:
            raise WorkflowError(
                f"approval_gate {gate.gate_name!r}.bound_behavior_on_timeout "
                f"invalid (must be one of {sorted(VALID_GATE_TIMEOUT_BEHAVIORS)})"
            )
        if (
            gate.bound_behavior_on_timeout == "auto_proceed_with_default"
            and gate.default_value is None
        ):
            raise WorkflowError(
                f"approval_gate {gate.gate_name!r} uses auto_proceed_with_default "
                f"but no default_value was declared"
            )
        if gate.timeout_seconds <= 0:
            raise WorkflowError(
                f"approval_gate {gate.gate_name!r}.timeout_seconds must be > 0"
            )
        validate_predicate(gate.approval_event_predicate)
    # gate_ref resolution.
    for idx, action in enumerate(wf.action_sequence):
        if action.gate_ref and action.gate_ref not in declared_gate_names:
            raise WorkflowError(
                f"action_sequence[{idx}].gate_ref {action.gate_ref!r} does not "
                f"resolve to any gate declared in approval_gates"
            )
    # Safe-deny: gates with auto_proceed_with_default cannot be followed by
    # an irreversible action before the next gate (or end).
    for idx, action in enumerate(wf.action_sequence):
        if action.gate_ref is None:
            continue
        gate = next(g for g in wf.approval_gates if g.gate_name == action.gate_ref)
        if gate.bound_behavior_on_timeout != "auto_proceed_with_default":
            continue
        # Walk subsequent actions until next gate or end.
        for downstream_idx in range(idx + 1, len(wf.action_sequence)):
            downstream = wf.action_sequence[downstream_idx]
            if downstream.gate_ref is not None:
                break  # next gate boundary reached
            if is_irreversible(downstream.action_type, downstream.parameters):
                raise WorkflowError(
                    f"approval_gate {gate.gate_name!r} uses "
                    f"auto_proceed_with_default but action_sequence"
                    f"[{downstream_idx}] ({downstream.action_type!r}) is "
                    f"irreversible — timeout would silently permit a "
                    f"world-effecting action without human approval"
                )
    # Trigger predicate.
    if wf.trigger is not None:
        validate_predicate(wf.trigger.predicate)
        if not wf.trigger.event_type:
            raise WorkflowError("trigger.event_type is required")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_WORKFLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    owner           TEXT DEFAULT '',
    version         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    descriptor_json TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflows_active
    ON workflows(instance_id, status);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _WORKFLOWS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    await db.commit()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _workflow_descriptor_blob(wf: Workflow) -> str:
    """Serialise the Workflow body that doesn't live in indexed
    columns. Excludes columns we store separately."""
    body = {
        "bounds": asdict(wf.bounds),
        "verifier": asdict(wf.verifier),
        "action_sequence": [asdict(a) for a in wf.action_sequence],
        "approval_gates": [asdict(g) for g in wf.approval_gates],
        "trigger": asdict(wf.trigger) if wf.trigger else None,
        "metadata": wf.metadata,
        "instance_local": wf.instance_local,
    }
    return json.dumps(body)


def _workflow_from_row(row) -> Workflow:
    body = json.loads(row["descriptor_json"])
    bounds = Bounds(**body["bounds"])
    verifier = Verifier(**body["verifier"])
    action_sequence = [
        ActionDescriptor(
            action_type=a["action_type"],
            parameters=a.get("parameters") or {},
            per_action_expectation=a.get("per_action_expectation", ""),
            continuation_rules=ContinuationRules(
                **(a.get("continuation_rules") or {})
            ),
            gate_ref=a.get("gate_ref"),
            resume_safe=a.get("resume_safe", False),
        )
        for a in body["action_sequence"]
    ]
    approval_gates = [ApprovalGate(**g) for g in body.get("approval_gates", [])]
    trigger_body = body.get("trigger")
    trigger = TriggerDescriptor(**trigger_body) if trigger_body else None
    return Workflow(
        workflow_id=row["workflow_id"],
        instance_id=row["instance_id"],
        name=row["name"],
        description=row["description"] or "",
        owner=row["owner"] or "",
        version=row["version"],
        status=row["status"],
        bounds=bounds,
        verifier=verifier,
        action_sequence=action_sequence,
        approval_gates=approval_gates,
        trigger=trigger,
        metadata=body.get("metadata") or {},
        created_at=row["created_at"],
        instance_local=body.get("instance_local", False),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class WorkflowRegistry:
    """Owns the workflows SQLite table and the cross-table atomic
    registration pipeline. Shares the TriggerRegistry's connection so
    workflow + trigger inserts run inside a single SQLite transaction.
    """

    def __init__(self) -> None:
        self._trigger_registry: TriggerRegistry | None = None
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self, trigger_registry: TriggerRegistry) -> None:
        """Adopt the trigger registry's connection and ensure schema."""
        if self._db is not None:
            return
        if trigger_registry._db is None:  # type: ignore[attr-defined]
            raise RuntimeError(
                "TriggerRegistry must be started before WorkflowRegistry"
            )
        self._trigger_registry = trigger_registry
        self._db = trigger_registry._db  # type: ignore[attr-defined]
        await _ensure_schema(self._db)

    async def stop(self) -> None:
        """Detach. The trigger registry owns the connection lifecycle."""
        self._db = None
        self._trigger_registry = None

    # -- registration ---------------------------------------------------

    async def register_workflow_from_file(self, file_path: str) -> Workflow:
        """Parse a portable descriptor file (.workflow.yaml/.json/.md)
        and atomically register it. Operator-facing entry point per
        the spec; tests can use ``register_workflow`` directly with a
        constructed :class:`Workflow`."""
        # Imported lazily to avoid a circular import: the parser module
        # imports the dataclasses defined here.
        from kernos.kernel.workflows.descriptor_parser import parse_descriptor

        wf = parse_descriptor(file_path)
        return await self.register_workflow(wf)

    async def register_workflow(self, wf: Workflow) -> Workflow:
        """Validate + atomically persist the workflow + its trigger.

        Both rows are written inside a single SQLite transaction. If
        any step raises (validation, persistence, trigger compilation,
        cache update), the transaction rolls back fully so no row is
        left behind.
        """
        if self._db is None or self._trigger_registry is None:
            raise RuntimeError("WorkflowRegistry not started")
        if not wf.workflow_id:
            wf.workflow_id = str(uuid.uuid4())
        if not wf.created_at:
            wf.created_at = datetime.now(timezone.utc).isoformat()
        # Validate before any I/O. Predicates inside (workflow trigger,
        # gate predicates) are also validated here.
        validate_workflow(wf)
        # Build the corresponding Trigger row.
        trigger: Trigger | None = None
        if wf.trigger is not None:
            trigger = Trigger(
                trigger_id=str(uuid.uuid4()),
                workflow_id=wf.workflow_id,
                instance_id=wf.instance_id,
                event_type=wf.trigger.event_type,
                predicate=wf.trigger.predicate,
                predicate_source=wf.trigger.predicate_source,
                description=wf.trigger.description,
                actor_filter=wf.trigger.actor_filter,
                correlation_filter=wf.trigger.correlation_filter,
                idempotency_key_template=wf.trigger.idempotency_key_template,
                owner=wf.owner,
                version=1,
                status="active",
                created_at=wf.created_at,
            )
        async with self._lock:
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    "INSERT INTO workflows ("
                    " workflow_id, instance_id, name, description, owner,"
                    " version, status, descriptor_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        wf.workflow_id, wf.instance_id, wf.name, wf.description,
                        wf.owner, wf.version, wf.status,
                        _workflow_descriptor_blob(wf), wf.created_at,
                    ),
                )
                if trigger is not None:
                    await self._db.execute(
                        "INSERT INTO triggers ("
                        " trigger_id, workflow_id, instance_id, event_type,"
                        " predicate, predicate_source, description, actor_filter,"
                        " correlation_filter, idempotency_key_template, owner,"
                        " version, status, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        trigger.to_row(),
                    )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
        # After the transaction commits, refresh the trigger registry's
        # in-memory cache so the new trigger fires on subsequent flushes.
        if trigger is not None:
            try:
                async with self._trigger_registry._cache_lock:  # type: ignore[attr-defined]
                    self._trigger_registry._cache_insert(trigger)  # type: ignore[attr-defined]
            except Exception:
                # Roll back the durable rows so on-disk and in-memory
                # state cannot disagree. If the rollback fails too,
                # surface both — the operator can inspect.
                logger.error(
                    "WORKFLOW_REGISTER_CACHE_FAILED workflow_id=%s error attempting rollback",
                    wf.workflow_id,
                )
                try:
                    await self._db.execute(
                        "DELETE FROM workflows WHERE workflow_id = ?",
                        (wf.workflow_id,),
                    )
                    await self._db.execute(
                        "DELETE FROM triggers WHERE trigger_id = ?",
                        (trigger.trigger_id,),
                    )
                    await self._db.commit()
                except Exception as rb_exc:
                    logger.error(
                        "WORKFLOW_REGISTER_ROLLBACK_FAILED workflow_id=%s error=%s",
                        wf.workflow_id, rb_exc, exc_info=True,
                    )
                raise
        return wf

    # -- queries --------------------------------------------------------

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,),
        ) as cur:
            row = await cur.fetchone()
        return _workflow_from_row(row) if row else None

    async def list_workflows(
        self, instance_id: str, *, status: str | None = None,
    ) -> list[Workflow]:
        if self._db is None:
            return []
        if status is None:
            query = "SELECT * FROM workflows WHERE instance_id = ? ORDER BY created_at"
            args: tuple = (instance_id,)
        else:
            query = (
                "SELECT * FROM workflows WHERE instance_id = ? AND status = ? "
                "ORDER BY created_at"
            )
            args = (instance_id, status)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [_workflow_from_row(r) for r in rows]

    async def update_status(self, workflow_id: str, status: str) -> bool:
        if self._db is None:
            return False
        if status not in VALID_WORKFLOW_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        async with self._lock:
            await self._db.execute(
                "UPDATE workflows SET status = ? WHERE workflow_id = ?",
                (status, workflow_id),
            )
            await self._db.commit()
        return True


__all__ = [
    "ActionDescriptor",
    "ApprovalGate",
    "Bounds",
    "ContinuationRules",
    "TriggerDescriptor",
    "Verifier",
    "Workflow",
    "WorkflowError",
    "WorkflowRegistry",
    "validate_workflow",
]
