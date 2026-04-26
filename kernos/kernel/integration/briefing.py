"""Briefing artifact + CohortOutput contract.

The briefing is the load-bearing contract between the integration
layer and the presence layer. Integration sees the full input
contract (raw cohort outputs, secret covenants, hidden memory,
restricted context-space material). Presence sees the briefing.
The schema here is the architecture's safety surface.

Per the revised INTEGRATION-LAYER spec (architect verdict
"REVISE TO MATCH REALITY", 2026-04-26):

- Section 1 fixes the Briefing schema: ContextItem array,
  FilteredItem array, tagged-union DecidedAction (six variants),
  bounded prose presence_directive, AuditTrace with references.

- Section 4 fixes the CohortOutput contract: cohort_id,
  cohort_run_id, output, visibility tagged union, produced_at.

- Section 3 (the load-bearing safety invariant): the briefing
  itself must be presence-safe. Integration uses restricted inputs
  to *shape* the briefing, but the briefing's text must contain
  no secret-covenant text, no hidden-memory content, no
  restricted context-space material. Restricted CohortOutputs
  carry a Visibility.Restricted tag so the runner can route them
  through the redaction path.

The schema cannot enforce semantic presence-safety automatically —
that's the runner's job — but it documents the contract clearly
so callers can't accidentally embed forbidden content.

The decided_action enum is final at:

    respond_only | execute_tool | propose_tool
                 | constrained_response | pivot | defer

All dataclasses are frozen — briefings and cohort outputs are
produced once and consumed read-only. Round-trip serialisation
lives on each class via `to_dict()` / `from_dict()` for the
audit-log pipeline.

source_type convention (free-form string per spec): dotted
prefix `<category>.<id>`. Examples:

  - "cohort.memory" / "cohort.weather" / "cohort.gardener"
  - "tool.read" or "tool.read.<tool_id>"
  - "context_space" or "context_space.<space_id>"
  - "conversation"

Schema validates non-empty; conventions are caller responsibility
so new cohorts can adopt the source_type space without spec churn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BriefingValidationError(ValueError):
    """Raised when briefing or cohort schema validation fails."""


# ---------------------------------------------------------------------------
# Visibility tagged union (CohortOutput safety property)
# ---------------------------------------------------------------------------


class VisibilityKind(str, Enum):
    """Discriminator for the Visibility tagged union.

    A CohortOutput tagged Public is safe to summarise into the
    briefing's text fields (after distillation). A CohortOutput
    tagged Restricted may shape integration's decision but its
    output content must NOT appear quoted in the briefing — only
    behavioral instruction in presence_directive can reflect it.
    """

    PUBLIC = "public"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class Public:
    kind: ClassVar[VisibilityKind] = VisibilityKind.PUBLIC

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Public":
        if not isinstance(data, dict) or data.get("kind") != cls.kind.value:
            raise BriefingValidationError(
                f"Public.from_dict expected kind=public; got {data!r}"
            )
        return cls()


@dataclass(frozen=True)
class Restricted:
    """Restricted visibility carries a `reason` string for audit.

    The reason is itself part of the audit surface — it should
    describe the policy that restricts the cohort output (e.g.
    "covenant", "hidden_memory", "cross_space"), not the secret
    content itself.
    """

    kind: ClassVar[VisibilityKind] = VisibilityKind.RESTRICTED
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Restricted.reason must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Restricted":
        if not isinstance(data, dict) or data.get("kind") != cls.kind.value:
            raise BriefingValidationError(
                f"Restricted.from_dict expected kind=restricted; got {data!r}"
            )
        return cls(reason=str(data.get("reason", "")))


Visibility = Public | Restricted


def visibility_from_dict(data: dict[str, Any]) -> Visibility:
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"visibility must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    raw = data.get("kind")
    try:
        kind = VisibilityKind(raw)
    except ValueError as exc:
        valid = ", ".join(k.value for k in VisibilityKind)
        raise BriefingValidationError(
            f"visibility.kind {raw!r} is not one of: {valid}"
        ) from exc
    if kind is VisibilityKind.PUBLIC:
        return Public.from_dict(data)
    return Restricted.from_dict(data)


# ---------------------------------------------------------------------------
# Outcome (CohortOutput runner-owned metadata; COHORT-FAN-OUT-RUNNER edit #4 + #8)
# ---------------------------------------------------------------------------


class Outcome(str, Enum):
    """Runner-owned outcome attribution for a CohortOutput.

    Synthetic outcomes (anything other than `success`) signal that the
    cohort did not produce its own output; the runner constructed the
    output to keep the result-list shape invariant. Integration's
    filter phase reads `outcome != success` to recognise failed
    cohorts.

    Per Kit edit #8: timeout cause is split into `timeout_per_cohort`
    (cohort exceeded its own `timeout_ms`) vs `timeout_global` (cohort
    cancelled because the global wall-clock cap was hit). The two
    cases differ in operator interpretation and downstream policy.
    """

    SUCCESS = "success"
    TIMEOUT_PER_COHORT = "timeout_per_cohort"
    TIMEOUT_GLOBAL = "timeout_global"
    ERROR = "error"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# CohortOutput
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortOutput:
    """Structured cohort output the integration runner consumes.

    The CohortOutput contract is part of the V1 spec because
    integration is the consumer; subsequent specs that build cohort
    adapters target this contract.

    `output` carries the cohort-specific payload. Integration's job
    is to interpret it; the schema only enforces that it's a dict
    so audit serialisation is straightforward. Restricted cohorts'
    output must NOT be copied into briefing text — see Visibility.

    Runner-owned metadata (added by COHORT-FAN-OUT-RUNNER, Kit edit
    #4): `outcome` and `error_summary`. These live OUTSIDE the
    cohort-specific `output` payload to avoid namespace collision —
    real cohorts may legitimately use a `status` key inside
    `output`. Synthetic CohortOutputs (timeout / error / cancelled)
    have `output: {}` and `outcome != Outcome.SUCCESS`; integration
    filters on `outcome` not on `output` content.
    """

    cohort_id: str
    cohort_run_id: str
    output: dict[str, Any]
    visibility: Visibility = field(default_factory=Public)
    produced_at: str = ""  # ISO 8601 UTC; runner fills if blank
    outcome: Outcome = Outcome.SUCCESS
    error_summary: str = ""  # populated for outcome != SUCCESS; redacted

    def __post_init__(self) -> None:
        if not isinstance(self.cohort_id, str) or not self.cohort_id.strip():
            raise BriefingValidationError(
                "CohortOutput.cohort_id must be a non-empty string"
            )
        if (
            not isinstance(self.cohort_run_id, str)
            or not self.cohort_run_id.strip()
        ):
            raise BriefingValidationError(
                "CohortOutput.cohort_run_id must be a non-empty string"
            )
        if not isinstance(self.output, dict):
            raise BriefingValidationError(
                f"CohortOutput.output must be a dict; got "
                f"{type(self.output).__name__}"
            )
        if not isinstance(self.visibility, (Public, Restricted)):
            raise BriefingValidationError(
                f"CohortOutput.visibility must be Public or Restricted; got "
                f"{type(self.visibility).__name__}"
            )
        if not isinstance(self.produced_at, str):
            raise BriefingValidationError(
                "CohortOutput.produced_at must be an ISO 8601 string"
            )
        if not isinstance(self.outcome, Outcome):
            raise BriefingValidationError(
                f"CohortOutput.outcome must be an Outcome; got "
                f"{type(self.outcome).__name__}"
            )
        if not isinstance(self.error_summary, str):
            raise BriefingValidationError(
                "CohortOutput.error_summary must be a string"
            )

    @property
    def is_restricted(self) -> bool:
        return isinstance(self.visibility, Restricted)

    @property
    def is_synthetic(self) -> bool:
        """True when the runner built this output (cohort failed or
        timed out). Integration filters on this rather than on
        `output` content."""
        return self.outcome is not Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "cohort_run_id": self.cohort_run_id,
            "output": dict(self.output),
            "visibility": self.visibility.to_dict(),
            "produced_at": self.produced_at,
            "outcome": self.outcome.value,
            "error_summary": self.error_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CohortOutput":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"CohortOutput must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        try:
            outcome = Outcome(data.get("outcome", Outcome.SUCCESS.value))
        except ValueError as exc:
            valid = ", ".join(o.value for o in Outcome)
            raise BriefingValidationError(
                f"CohortOutput.outcome {data.get('outcome')!r} is not one "
                f"of: {valid}"
            ) from exc
        return cls(
            cohort_id=str(data.get("cohort_id", "")),
            cohort_run_id=str(data.get("cohort_run_id", "")),
            output=dict(data.get("output", {}) or {}),
            visibility=visibility_from_dict(
                data.get("visibility") or Public().to_dict()
            ),
            produced_at=str(data.get("produced_at", "")),
            outcome=outcome,
            error_summary=str(data.get("error_summary", "")),
        )


def now_iso() -> str:
    """Helper: ISO 8601 UTC timestamp for CohortOutput.produced_at."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Source type validation
# ---------------------------------------------------------------------------


def _validate_source_type(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BriefingValidationError(
            f"{field_name} must be a non-empty string (e.g. "
            f"'cohort.memory', 'tool.read', 'context_space')"
        )
    return value.strip()


# ---------------------------------------------------------------------------
# ContextItem / FilteredItem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextItem:
    """A single relevant piece of context surfaced into the briefing.

    `summary` MUST be presence-safe. If the underlying material is
    a Restricted cohort or hidden memory, the summary carries the
    behavioral implication, not the source content. The runner is
    responsible for that translation; this dataclass is the surface
    presence sees.
    """

    source_type: str
    source_id: str
    summary: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_type",
            _validate_source_type(self.source_type, "ContextItem.source_type"),
        )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise BriefingValidationError(
                "ContextItem.source_id must be a non-empty string"
            )
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise BriefingValidationError(
                "ContextItem.summary must be a non-empty string"
            )
        if not isinstance(self.confidence, (int, float)):
            raise BriefingValidationError(
                "ContextItem.confidence must be a number in [0.0, 1.0]"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise BriefingValidationError(
                f"ContextItem.confidence must be in [0.0, 1.0]; got "
                f"{self.confidence}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "summary": self.summary,
            "confidence": float(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextItem":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"ContextItem must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            source_type=str(data.get("source_type", "")),
            source_id=str(data.get("source_id", "")),
            summary=str(data.get("summary", "")),
            confidence=float(data.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class FilteredItem:
    """A piece of context integration considered and dismissed.

    Per spec Section 1: source_type, source_id, reason_filtered.
    No summary field — the filtered audit trail records what was
    weighed and why it was dismissed; downstream auditors can
    cross-reference the source by source_id if they need detail.
    """

    source_type: str
    source_id: str
    reason_filtered: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_type",
            _validate_source_type(self.source_type, "FilteredItem.source_type"),
        )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise BriefingValidationError(
                "FilteredItem.source_id must be a non-empty string"
            )
        if (
            not isinstance(self.reason_filtered, str)
            or not self.reason_filtered.strip()
        ):
            raise BriefingValidationError(
                "FilteredItem.reason_filtered must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "reason_filtered": self.reason_filtered,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilteredItem":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"FilteredItem must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            source_type=str(data.get("source_type", "")),
            source_id=str(data.get("source_id", "")),
            reason_filtered=str(data.get("reason_filtered", "")),
        )


# ---------------------------------------------------------------------------
# DecidedAction tagged union
# ---------------------------------------------------------------------------


class ActionKind(str, Enum):
    """Discriminator for the DecidedAction tagged union.

    Final per the revised spec, exactly six variants:
      - respond_only          presence generates a conversational reply
      - execute_tool          presence executes the named tool now
      - propose_tool          presence surfaces a confirmation to the user
      - constrained_response  presence generates partial satisfaction
                              under a named limit
      - pivot                 presence generates a different shape than
                              the literal request
      - defer                 presence acknowledges and signals delay
    """

    RESPOND_ONLY = "respond_only"
    EXECUTE_TOOL = "execute_tool"
    PROPOSE_TOOL = "propose_tool"
    CONSTRAINED_RESPONSE = "constrained_response"
    PIVOT = "pivot"
    DEFER = "defer"


@dataclass(frozen=True)
class RespondOnly:
    kind: ClassVar[ActionKind] = ActionKind.RESPOND_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RespondOnly":
        _expect_kind(data, cls.kind)
        return cls()


@dataclass(frozen=True)
class ExecuteTool:
    kind: ClassVar[ActionKind] = ActionKind.EXECUTE_TOOL
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    narration_context: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            raise BriefingValidationError(
                "ExecuteTool.tool_id must be a non-empty string"
            )
        if not isinstance(self.arguments, dict):
            raise BriefingValidationError(
                f"ExecuteTool.arguments must be a dict; got "
                f"{type(self.arguments).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tool_id": self.tool_id,
            "arguments": dict(self.arguments),
            "narration_context": self.narration_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecuteTool":
        _expect_kind(data, cls.kind)
        return cls(
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {}) or {}),
            narration_context=str(data.get("narration_context", "")),
        )


@dataclass(frozen=True)
class ProposeTool:
    kind: ClassVar[ActionKind] = ActionKind.PROPOSE_TOOL
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            raise BriefingValidationError(
                "ProposeTool.tool_id must be a non-empty string"
            )
        if not isinstance(self.arguments, dict):
            raise BriefingValidationError(
                f"ProposeTool.arguments must be a dict; got "
                f"{type(self.arguments).__name__}"
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "ProposeTool.reason must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tool_id": self.tool_id,
            "arguments": dict(self.arguments),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposeTool":
        _expect_kind(data, cls.kind)
        return cls(
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {}) or {}),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class ConstrainedResponse:
    """Presence generates partial satisfaction under a named limit.

    Per revised spec Section 1: fields are constraint and
    satisfaction_partial (renamed from earlier draft's
    partial_satisfaction).
    """

    kind: ClassVar[ActionKind] = ActionKind.CONSTRAINED_RESPONSE
    constraint: str = ""
    satisfaction_partial: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.constraint, str) or not self.constraint.strip():
            raise BriefingValidationError(
                "ConstrainedResponse.constraint must be a non-empty string"
            )
        if (
            not isinstance(self.satisfaction_partial, str)
            or not self.satisfaction_partial.strip()
        ):
            raise BriefingValidationError(
                "ConstrainedResponse.satisfaction_partial must be a "
                "non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "constraint": self.constraint,
            "satisfaction_partial": self.satisfaction_partial,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConstrainedResponse":
        _expect_kind(data, cls.kind)
        return cls(
            constraint=str(data.get("constraint", "")),
            satisfaction_partial=str(data.get("satisfaction_partial", "")),
        )


@dataclass(frozen=True)
class Pivot:
    """Presence generates a different shape than the literal request.

    Per revised spec Section 1: reason + suggested_shape. reason is
    presence-safe behavioral framing; suggested_shape names the
    redirected response shape (e.g. "general planning conversation"
    rather than "the surprise party topic").
    """

    kind: ClassVar[ActionKind] = ActionKind.PIVOT
    reason: str = ""
    suggested_shape: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Pivot.reason must be a non-empty string"
            )
        if (
            not isinstance(self.suggested_shape, str)
            or not self.suggested_shape.strip()
        ):
            raise BriefingValidationError(
                "Pivot.suggested_shape must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "suggested_shape": self.suggested_shape,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pivot":
        _expect_kind(data, cls.kind)
        return cls(
            reason=str(data.get("reason", "")),
            suggested_shape=str(data.get("suggested_shape", "")),
        )


@dataclass(frozen=True)
class Defer:
    """Presence acknowledges and signals delay or follow-up.

    Per revised spec Section 1: reason + follow_up_signal.
    follow_up_signal carries the specific marker presence should
    surface to the user (e.g. "I'll come back when the build
    finishes" or "queued; will follow up tomorrow").
    """

    kind: ClassVar[ActionKind] = ActionKind.DEFER
    reason: str = ""
    follow_up_signal: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Defer.reason must be a non-empty string"
            )
        if (
            not isinstance(self.follow_up_signal, str)
            or not self.follow_up_signal.strip()
        ):
            raise BriefingValidationError(
                "Defer.follow_up_signal must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "follow_up_signal": self.follow_up_signal,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Defer":
        _expect_kind(data, cls.kind)
        return cls(
            reason=str(data.get("reason", "")),
            follow_up_signal=str(data.get("follow_up_signal", "")),
        )


DecidedAction = (
    RespondOnly | ExecuteTool | ProposeTool | ConstrainedResponse | Pivot | Defer
)


_ACTION_VARIANTS: dict[ActionKind, type] = {
    ActionKind.RESPOND_ONLY: RespondOnly,
    ActionKind.EXECUTE_TOOL: ExecuteTool,
    ActionKind.PROPOSE_TOOL: ProposeTool,
    ActionKind.CONSTRAINED_RESPONSE: ConstrainedResponse,
    ActionKind.PIVOT: Pivot,
    ActionKind.DEFER: Defer,
}


def _expect_kind(data: dict[str, Any], expected: ActionKind) -> None:
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"action variant must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    actual = data.get("kind")
    if actual != expected.value:
        raise BriefingValidationError(
            f"action variant kind mismatch: expected {expected.value!r}, "
            f"got {actual!r}"
        )


def decided_action_from_dict(data: dict[str, Any]) -> DecidedAction:
    """Dispatch on `kind` and parse the matching DecidedAction variant."""
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"decided_action must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    raw_kind = data.get("kind")
    try:
        kind = ActionKind(raw_kind)
    except ValueError as exc:
        valid = ", ".join(k.value for k in ActionKind)
        raise BriefingValidationError(
            f"decided_action.kind {raw_kind!r} is not one of: {valid}"
        ) from exc
    return _ACTION_VARIANTS[kind].from_dict(data)


# ---------------------------------------------------------------------------
# BudgetState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetState:
    """Which limits the integration run hit, if any.

    V1 (INTEGRATION-LAYER-V1) Section 4c flags: iterations,
    timeout, cohort_entries, filtered_entries, tokens.

    COHORT-FAN-OUT-RUNNER Section 8 adds three flags integration's
    filter phase reads to apply downstream policy:

      - required_cohort_failed: at least one required cohort's
        outcome is not SUCCESS. Integration produces a constrained
        briefing that notes degraded context.
      - required_safety_cohort_failed: a required cohort with
        safety_class True failed. Integration's decided action
        defaults to constrained_response or defer rather than
        respond_only. Principle: if safety can't be verified, we
        don't proceed at full strength.
      - cohort_fan_out_global_timeout: the fan-out runner hit its
        wall-clock cap. Distinct from the other failure flags so
        downstream telemetry can attribute global vs per-cohort
        causes cleanly.

    `tokens_hit_limit` is provisional — the integration runner
    doesn't track cumulative token usage across iterations in v1
    (max_tokens is per-call). The flag is in the schema so the
    field exists when cumulative tracking lands without breaking
    serialised audit records.
    """

    iterations_hit_limit: bool = False
    timeout_hit_limit: bool = False
    cohort_entries_hit_limit: bool = False
    filtered_entries_hit_limit: bool = False
    tokens_hit_limit: bool = False
    required_cohort_failed: bool = False
    required_safety_cohort_failed: bool = False
    cohort_fan_out_global_timeout: bool = False

    def __post_init__(self) -> None:
        for name in (
            "iterations_hit_limit",
            "timeout_hit_limit",
            "cohort_entries_hit_limit",
            "filtered_entries_hit_limit",
            "tokens_hit_limit",
            "required_cohort_failed",
            "required_safety_cohort_failed",
            "cohort_fan_out_global_timeout",
        ):
            if not isinstance(getattr(self, name), bool):
                raise BriefingValidationError(
                    f"BudgetState.{name} must be a bool"
                )

    @property
    def any_hit(self) -> bool:
        return (
            self.iterations_hit_limit
            or self.timeout_hit_limit
            or self.cohort_entries_hit_limit
            or self.filtered_entries_hit_limit
            or self.tokens_hit_limit
            or self.required_cohort_failed
            or self.required_safety_cohort_failed
            or self.cohort_fan_out_global_timeout
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations_hit_limit": self.iterations_hit_limit,
            "timeout_hit_limit": self.timeout_hit_limit,
            "cohort_entries_hit_limit": self.cohort_entries_hit_limit,
            "filtered_entries_hit_limit": self.filtered_entries_hit_limit,
            "tokens_hit_limit": self.tokens_hit_limit,
            "required_cohort_failed": self.required_cohort_failed,
            "required_safety_cohort_failed": (
                self.required_safety_cohort_failed
            ),
            "cohort_fan_out_global_timeout": (
                self.cohort_fan_out_global_timeout
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetState":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"BudgetState must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            iterations_hit_limit=bool(data.get("iterations_hit_limit", False)),
            timeout_hit_limit=bool(data.get("timeout_hit_limit", False)),
            cohort_entries_hit_limit=bool(
                data.get("cohort_entries_hit_limit", False)
            ),
            filtered_entries_hit_limit=bool(
                data.get("filtered_entries_hit_limit", False)
            ),
            tokens_hit_limit=bool(data.get("tokens_hit_limit", False)),
            required_cohort_failed=bool(
                data.get("required_cohort_failed", False)
            ),
            required_safety_cohort_failed=bool(
                data.get("required_safety_cohort_failed", False)
            ),
            cohort_fan_out_global_timeout=bool(
                data.get("cohort_fan_out_global_timeout", False)
            ),
        )


# ---------------------------------------------------------------------------
# AuditTrace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditTrace:
    """References + telemetry for the integration run.

    Per revised spec Section 1: cohort_outputs (references to the
    cohort outputs the run consumed; not raw dumps),
    tools_called_during_prep (references to read-only tool
    invocations during integration), iterations_used,
    budget_state, fail_soft_engaged.

    `phase_durations_ms` and `notes` are telemetry not in the
    minimal schema but required for acceptance criterion #12
    ("internal phases observable in instrumentation"). They
    carry per-phase millisecond timings keyed by phase name (the
    five named phases plus per-iteration markers) and a free-form
    notes string the runner uses to record fail-soft cause.
    """

    cohort_outputs: tuple[str, ...] = ()
    tools_called_during_prep: tuple[str, ...] = ()
    iterations_used: int = 0
    budget_state: BudgetState = field(default_factory=BudgetState)
    fail_soft_engaged: bool = False
    phase_durations_ms: dict[str, int] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        for ref in self.cohort_outputs:
            if not isinstance(ref, str) or not ref.strip():
                raise BriefingValidationError(
                    "AuditTrace.cohort_outputs entries must be non-empty "
                    "strings"
                )
        for ref in self.tools_called_during_prep:
            if not isinstance(ref, str) or not ref.strip():
                raise BriefingValidationError(
                    "AuditTrace.tools_called_during_prep entries must be "
                    "non-empty strings"
                )
        if (
            not isinstance(self.iterations_used, int)
            or self.iterations_used < 0
        ):
            raise BriefingValidationError(
                "AuditTrace.iterations_used must be a non-negative int"
            )
        if not isinstance(self.budget_state, BudgetState):
            raise BriefingValidationError(
                f"AuditTrace.budget_state must be a BudgetState; got "
                f"{type(self.budget_state).__name__}"
            )
        if not isinstance(self.fail_soft_engaged, bool):
            raise BriefingValidationError(
                "AuditTrace.fail_soft_engaged must be a bool"
            )
        if not isinstance(self.phase_durations_ms, dict):
            raise BriefingValidationError(
                "AuditTrace.phase_durations_ms must be a dict[str, int]"
            )
        for phase, dur in self.phase_durations_ms.items():
            if not isinstance(phase, str) or not phase.strip():
                raise BriefingValidationError(
                    "AuditTrace.phase_durations_ms keys must be non-empty "
                    "strings"
                )
            if not isinstance(dur, int) or dur < 0:
                raise BriefingValidationError(
                    f"AuditTrace.phase_durations_ms[{phase!r}] must be a "
                    f"non-negative int (milliseconds)"
                )
        if not isinstance(self.notes, str):
            raise BriefingValidationError("AuditTrace.notes must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_outputs": list(self.cohort_outputs),
            "tools_called_during_prep": list(self.tools_called_during_prep),
            "iterations_used": self.iterations_used,
            "budget_state": self.budget_state.to_dict(),
            "fail_soft_engaged": self.fail_soft_engaged,
            "phase_durations_ms": dict(self.phase_durations_ms),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditTrace":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"AuditTrace must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            cohort_outputs=tuple(data.get("cohort_outputs", []) or []),
            tools_called_during_prep=tuple(
                data.get("tools_called_during_prep", []) or []
            ),
            iterations_used=int(data.get("iterations_used", 0)),
            budget_state=BudgetState.from_dict(
                data.get("budget_state") or {}
            ),
            fail_soft_engaged=bool(data.get("fail_soft_engaged", False)),
            phase_durations_ms=dict(data.get("phase_durations_ms", {}) or {}),
            notes=str(data.get("notes", "")),
        )


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Briefing:
    """The artifact integration hands to presence.

    Carries the architecture's safety property: every text field is
    expected to be presence-safe. Integration's runner is responsible
    for redacting Restricted CohortOutput content before populating
    these fields.
    """

    relevant_context: tuple[ContextItem, ...]
    filtered_context: tuple[FilteredItem, ...]
    decided_action: DecidedAction
    presence_directive: str
    audit_trace: AuditTrace
    turn_id: str = ""
    integration_run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.relevant_context, tuple):
            raise BriefingValidationError(
                "Briefing.relevant_context must be a tuple of ContextItem"
            )
        for item in self.relevant_context:
            if not isinstance(item, ContextItem):
                raise BriefingValidationError(
                    f"Briefing.relevant_context entries must be ContextItem; "
                    f"got {type(item).__name__}"
                )
        if not isinstance(self.filtered_context, tuple):
            raise BriefingValidationError(
                "Briefing.filtered_context must be a tuple of FilteredItem"
            )
        for item in self.filtered_context:
            if not isinstance(item, FilteredItem):
                raise BriefingValidationError(
                    f"Briefing.filtered_context entries must be FilteredItem; "
                    f"got {type(item).__name__}"
                )
        if not isinstance(
            self.decided_action,
            (
                RespondOnly,
                ExecuteTool,
                ProposeTool,
                ConstrainedResponse,
                Pivot,
                Defer,
            ),
        ):
            raise BriefingValidationError(
                f"Briefing.decided_action must be a DecidedAction variant; "
                f"got {type(self.decided_action).__name__}"
            )
        if not isinstance(self.presence_directive, str):
            raise BriefingValidationError(
                "Briefing.presence_directive must be a string"
            )
        if not self.presence_directive.strip():
            raise BriefingValidationError(
                "Briefing.presence_directive must be a non-empty string "
                "(presence needs framing every turn)"
            )
        if not isinstance(self.audit_trace, AuditTrace):
            raise BriefingValidationError(
                f"Briefing.audit_trace must be an AuditTrace; got "
                f"{type(self.audit_trace).__name__}"
            )
        if not isinstance(self.turn_id, str):
            raise BriefingValidationError("Briefing.turn_id must be a string")
        if not isinstance(self.integration_run_id, str):
            raise BriefingValidationError(
                "Briefing.integration_run_id must be a string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relevant_context": [item.to_dict() for item in self.relevant_context],
            "filtered_context": [item.to_dict() for item in self.filtered_context],
            "decided_action": self.decided_action.to_dict(),
            "presence_directive": self.presence_directive,
            "audit_trace": self.audit_trace.to_dict(),
            "turn_id": self.turn_id,
            "integration_run_id": self.integration_run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Briefing":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"Briefing must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            relevant_context=tuple(
                ContextItem.from_dict(d)
                for d in (data.get("relevant_context") or [])
            ),
            filtered_context=tuple(
                FilteredItem.from_dict(d)
                for d in (data.get("filtered_context") or [])
            ),
            decided_action=decided_action_from_dict(
                data.get("decided_action") or {}
            ),
            presence_directive=str(data.get("presence_directive", "")),
            audit_trace=AuditTrace.from_dict(data.get("audit_trace") or {}),
            turn_id=str(data.get("turn_id", "")),
            integration_run_id=str(data.get("integration_run_id", "")),
        )


# ---------------------------------------------------------------------------
# Fail-soft fallback
# ---------------------------------------------------------------------------


_FAIL_SOFT_DIRECTIVE = (
    "integration prep was incomplete; respond conservatively and "
    "acknowledge limited context if relevant."
)


def minimal_fail_soft_briefing(
    *,
    turn_id: str = "",
    integration_run_id: str = "",
    notes: str = "",
    budget_state: BudgetState | None = None,
) -> Briefing:
    """Construct the minimal briefing the runner returns on failure.

    Per Section 4c of the spec: if integration fails, errors, or
    exceeds budget, presence receives a minimal `respond_only`
    briefing acknowledging incomplete prep — never raw cohort
    inputs. The directive is the spec's literal phrasing so
    presence can rely on its shape.
    """
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive=_FAIL_SOFT_DIRECTIVE,
        audit_trace=AuditTrace(
            iterations_used=0,
            budget_state=budget_state or BudgetState(),
            fail_soft_engaged=True,
            notes=notes,
        ),
        turn_id=turn_id,
        integration_run_id=integration_run_id,
    )
