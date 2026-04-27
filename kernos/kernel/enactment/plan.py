"""Plan + Step + StepExpectation + StructuredSignal vocabulary (PDI C5).

The vocabulary is **permanent** for the life of the system. Action-tool
authors write expectations against this contract; signal-kind names
and arg shapes do not change without a coordinated migration.

Per PDI Section 5h:
  - Plan: ordered tuple of Steps with a stable plan_id.
  - Step: action-tool-id + arguments + expectation.
  - StepExpectation: prose (always present) + structured signals
    (optional list of seven canonical kinds).
  - StructuredSignal: a (kind, args) pair; runtime checks
    deterministically when possible, falls to model-judged prose
    comparison for anything outside the seven.

Seven canonical signal kinds (locked):

  count_at_least    — collection at the named path has length ≥ value
  count_at_most     — collection at the named path has length ≤ value
  contains_field    — dict at the named path has key set
  returns_truthy    — result is truthy (non-empty, non-zero, etc.)
  success_status    — well-known success-status field equals value
  value_equality    — value at the named path equals expected
  value_in_set      — value at the named path is in the allowed set

Continuous numeric ranges and negative assertions intentionally fall
to prose comparison — adding a `value_in_range` or a negation operator
would fragment the vocabulary; the cases are uncommon enough that
prose model-judgment is acceptable.

Audit references-not-dumps invariant: plan payloads do NOT get
embedded in audit entries. The plan_id is the reference; the full
plan is reconstructible from the in-memory PlanLedger (lifecycle:
turn-scoped) or from the producer audit category if the caller
chooses to persist there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanValidationError(ValueError):
    """Raised when plan / step / expectation validation fails."""


# ---------------------------------------------------------------------------
# StructuredSignal — the seven canonical kinds
# ---------------------------------------------------------------------------


class SignalKind(str, Enum):
    """Closed enum of expectation signal kinds. Permanent vocabulary."""

    COUNT_AT_LEAST = "count_at_least"
    COUNT_AT_MOST = "count_at_most"
    CONTAINS_FIELD = "contains_field"
    RETURNS_TRUTHY = "returns_truthy"
    SUCCESS_STATUS = "success_status"
    VALUE_EQUALITY = "value_equality"
    VALUE_IN_SET = "value_in_set"


@dataclass(frozen=True)
class StructuredSignal:
    """A single deterministically-checkable expectation signal.

    `args` is the signal's parameters. Each kind has a documented
    arg shape:

      count_at_least  : {"path": str (default ""), "value": int}
      count_at_most   : {"path": str (default ""), "value": int}
      contains_field  : {"path": str (default ""), "key": str}
      returns_truthy  : {"path": str (default "")}
      success_status  : {"path": str (default "status"|"ok"), "value": Any}
                        — defaults: path "ok", value True (covers the
                        common `{"ok": True}` shape)
      value_equality  : {"path": str (default ""), "value": Any}
      value_in_set    : {"path": str (default ""), "values": list}

    `path` semantics: dotted-key path into the result dict. Empty
    string addresses the root. Missing intermediate keys produce a
    "signal failed" rather than an exception (signals are about
    expectation matching, not result-shape validation).
    """

    kind: SignalKind
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SignalKind):
            raise PlanValidationError(
                f"StructuredSignal.kind must be a SignalKind; got "
                f"{type(self.kind).__name__}"
            )
        if not isinstance(self.args, dict):
            raise PlanValidationError(
                "StructuredSignal.args must be a dict"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "args": dict(self.args)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StructuredSignal":
        try:
            kind = SignalKind(data.get("kind"))
        except ValueError as exc:
            valid = ", ".join(s.value for s in SignalKind)
            raise PlanValidationError(
                f"StructuredSignal.kind {data.get('kind')!r} is not one "
                f"of: {valid}"
            ) from exc
        return cls(kind=kind, args=dict(data.get("args") or {}))


# ---------------------------------------------------------------------------
# Signal evaluator — deterministic checks
# ---------------------------------------------------------------------------


def _resolve_path(result: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path into a result. Returns (found, value).

    Empty path addresses the root. Missing intermediate keys return
    (False, None) — signals interpret "missing" as a failed match,
    never an exception.
    """
    if not path:
        return True, result
    cursor: Any = result
    for part in path.split("."):
        if not isinstance(cursor, Mapping):
            return False, None
        if part not in cursor:
            return False, None
        cursor = cursor[part]
    return True, cursor


def evaluate_signal(
    signal: StructuredSignal, result: Any
) -> bool:
    """Evaluate a structured signal against a tool-call result.

    Returns True if the expectation matches deterministically; False
    if it diverges. Never raises for missing fields — missing means
    diverged. Falls back to a conservative False on unknown kinds so
    callers can route to model-judged prose comparison.
    """
    args = signal.args
    path = str(args.get("path", "")).strip()

    if signal.kind is SignalKind.COUNT_AT_LEAST:
        found, value = _resolve_path(result, path)
        if not found or not _is_sized(value):
            return False
        return len(value) >= int(args.get("value", 0))

    if signal.kind is SignalKind.COUNT_AT_MOST:
        found, value = _resolve_path(result, path)
        if not found or not _is_sized(value):
            return False
        return len(value) <= int(args.get("value", 0))

    if signal.kind is SignalKind.CONTAINS_FIELD:
        found, value = _resolve_path(result, path)
        if not found or not isinstance(value, Mapping):
            return False
        return str(args.get("key", "")) in value

    if signal.kind is SignalKind.RETURNS_TRUTHY:
        found, value = _resolve_path(result, path)
        if not found:
            return False
        return bool(value)

    if signal.kind is SignalKind.SUCCESS_STATUS:
        # Default path "ok", default value True. Covers the common
        # tool-result shape `{"ok": True}`.
        status_path = path or str(args.get("path", "ok") or "ok")
        found, value = _resolve_path(result, status_path)
        if not found:
            return False
        expected = args.get("value", True)
        return value == expected

    if signal.kind is SignalKind.VALUE_EQUALITY:
        found, value = _resolve_path(result, path)
        if not found:
            return False
        return value == args.get("value")

    if signal.kind is SignalKind.VALUE_IN_SET:
        found, value = _resolve_path(result, path)
        if not found:
            return False
        return value in (args.get("values") or [])

    # Unknown kind — conservative False; caller routes to prose.
    return False


def _is_sized(value: Any) -> bool:
    """True when len(value) is meaningful — covers list/tuple/set/
    dict/str. Excludes bool/int (which would len-error)."""
    return hasattr(value, "__len__") and not isinstance(value, bool)


def evaluate_expectation_signals(
    expectation: "StepExpectation", result: Any
) -> tuple[bool, tuple[StructuredSignal, ...]]:
    """Evaluate every structured signal on an expectation. Returns
    (all_passed, failed_signals). When `structured` is empty, returns
    (True, ()) — the prose layer takes over the divergence judgment
    entirely (model-judged)."""
    if not expectation.structured:
        return True, ()
    failures: list[StructuredSignal] = []
    for sig in expectation.structured:
        if not evaluate_signal(sig, result):
            failures.append(sig)
    return (not failures), tuple(failures)


# ---------------------------------------------------------------------------
# StepExpectation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepExpectation:
    """Hybrid expectation: prose (always) + optional structured signals.

    Prose is the human-readable expectation framing; the model judges
    divergence against it when structured signals are absent or pass
    but a deeper "did the effect match" question still applies.

    Structured signals are deterministic — runtime checks them without
    a model call. Passing all signals is a strong proceed signal;
    failing one is a strong divergence signal.
    """

    prose: str
    structured: tuple[StructuredSignal, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.prose, str) or not self.prose.strip():
            raise PlanValidationError(
                "StepExpectation.prose must be a non-empty string"
            )
        if not isinstance(self.structured, tuple):
            raise PlanValidationError(
                "StepExpectation.structured must be a tuple of "
                "StructuredSignal"
            )
        for sig in self.structured:
            if not isinstance(sig, StructuredSignal):
                raise PlanValidationError(
                    f"StepExpectation.structured entries must be "
                    f"StructuredSignal; got {type(sig).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prose": self.prose,
            "structured": [s.to_dict() for s in self.structured],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepExpectation":
        return cls(
            prose=str(data.get("prose", "")),
            structured=tuple(
                StructuredSignal.from_dict(s)
                for s in (data.get("structured") or [])
            ),
        )


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One [action, expectation] pair within a plan.

    `tool_id`: workshop tool name to dispatch.
    `arguments`: arg dict for the dispatch call.
    `tool_class`: capability class for envelope validation
                  (e.g. "email", "calendar"). Required for the
                  envelope check; "" is valid only when the envelope
                  permits an empty allowed_tool_classes (rare).
    `operation_name`: resolved at plan-creation time via the
                      operation_resolver (PDI C1). Required for
                      envelope.allowed_operations check. May be ""
                      only when the tool's classification is
                      single-operation and the resolver returns it.
    `expectation`: structured + prose.
    """

    step_id: str
    tool_id: str
    arguments: dict[str, Any]
    tool_class: str
    operation_name: str
    expectation: StepExpectation

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise PlanValidationError(
                "Step.step_id must be a non-empty string"
            )
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            raise PlanValidationError(
                "Step.tool_id must be a non-empty string"
            )
        if not isinstance(self.arguments, dict):
            raise PlanValidationError("Step.arguments must be a dict")
        if not isinstance(self.tool_class, str):
            raise PlanValidationError("Step.tool_class must be a string")
        if not isinstance(self.operation_name, str):
            raise PlanValidationError(
                "Step.operation_name must be a string"
            )
        if not isinstance(self.expectation, StepExpectation):
            raise PlanValidationError(
                "Step.expectation must be a StepExpectation"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool_id": self.tool_id,
            "arguments": dict(self.arguments),
            "tool_class": self.tool_class,
            "operation_name": self.operation_name,
            "expectation": self.expectation.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Step":
        return cls(
            step_id=str(data.get("step_id", "")),
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {}) or {}),
            tool_class=str(data.get("tool_class", "")),
            operation_name=str(data.get("operation_name", "")),
            expectation=StepExpectation.from_dict(
                data.get("expectation") or {"prose": " "}
            ),
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def new_plan_id() -> str:
    """Generate a stable plan_id. UUID-style; 32-char hex without dashes."""
    return uuid.uuid4().hex


def now_iso() -> str:
    """ISO 8601 UTC timestamp helper."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Plan:
    """Ordered tuple of Steps with stable identification.

    `plan_id`: stable identifier; audit entries reference this rather
               than embedding the plan payload (V1 references-not-dumps
               invariant).
    `turn_id`: forwarded for audit cross-reference.
    `created_at`: ISO 8601 UTC.
    `steps`: ordered tuple. Empty plans are explicitly invalid —
             integration produces a different decided_action_kind
             when there is no action to take.
    `created_via`: operator-readable label for plan provenance
                   (`initial`, `tier_4_reassemble`). Lets audit
                   distinguish initial plans from reassembled ones
                   without re-deriving from event order.
    """

    plan_id: str
    turn_id: str
    steps: tuple[Step, ...]
    created_at: str = ""
    created_via: str = "initial"

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise PlanValidationError(
                "Plan.plan_id must be a non-empty string"
            )
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise PlanValidationError(
                "Plan.turn_id must be a non-empty string"
            )
        if not isinstance(self.steps, tuple) or not self.steps:
            raise PlanValidationError(
                "Plan.steps must be a non-empty tuple of Step"
            )
        for step in self.steps:
            if not isinstance(step, Step):
                raise PlanValidationError(
                    f"Plan.steps entries must be Step; got "
                    f"{type(step).__name__}"
                )
        if not isinstance(self.created_at, str):
            raise PlanValidationError("Plan.created_at must be a string")
        if not isinstance(self.created_via, str) or not self.created_via.strip():
            raise PlanValidationError(
                "Plan.created_via must be a non-empty string "
                "(e.g. 'initial', 'tier_4_reassemble')"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "turn_id": self.turn_id,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "created_via": self.created_via,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        return cls(
            plan_id=str(data.get("plan_id", "")),
            turn_id=str(data.get("turn_id", "")),
            steps=tuple(
                Step.from_dict(s) for s in (data.get("steps") or [])
            ),
            created_at=str(data.get("created_at", "")),
            created_via=str(data.get("created_via", "initial")),
        )


__all__ = [
    "Plan",
    "PlanValidationError",
    "SignalKind",
    "Step",
    "StepExpectation",
    "StructuredSignal",
    "evaluate_expectation_signals",
    "evaluate_signal",
    "new_plan_id",
    "now_iso",
]
