"""Tests for envelope validation (PDI C5).

Each rule is a separate test. The envelope-validation function is the
structural enforcer of "EnactmentService never changes decided_action";
its rules are load-bearing.

Rules covered:
  1. Step's tool_class must be in envelope.allowed_tool_classes.
  2. Step's operation_name must be in envelope.allowed_operations.
  3. Step must not match any envelope.forbidden_moves.
  4. Plan must not bypass envelope.confirmation_requirements
     (require-confirmation operation cannot dispatch in same turn).
  5. Empty plans rejected.
"""

from __future__ import annotations

from kernos.kernel.enactment.envelope import (
    REASON_CONFIRMATION_BYPASS,
    REASON_FORBIDDEN_MOVE_MATCHED,
    REASON_OPERATION_NOT_ALLOWED,
    REASON_PLAN_EMPTY,
    REASON_TOOL_CLASS_NOT_ALLOWED,
    validate_plan_against_envelope,
    validate_step_against_envelope,
)
from kernos.kernel.enactment.plan import (
    Plan,
    Step,
    StepExpectation,
)
from kernos.kernel.integration.briefing import ActionEnvelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    *,
    step_id: str = "s1",
    tool_id: str = "email_send",
    tool_class: str = "email",
    operation_name: str = "send",
    arguments: dict | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        tool_id=tool_id,
        arguments=arguments or {},
        tool_class=tool_class,
        operation_name=operation_name,
        expectation=StepExpectation(prose="x"),
    )


def _plan(steps) -> Plan:
    return Plan(plan_id="p1", turn_id="t1", steps=tuple(steps))


# ---------------------------------------------------------------------------
# Step-level rules
# ---------------------------------------------------------------------------


def test_step_passes_when_tool_class_and_operation_match():
    envelope = ActionEnvelope(
        intended_outcome="send the email",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
    )
    outcome = validate_step_against_envelope(_step(), envelope)
    assert outcome.valid is True
    assert outcome.reason == ""


def test_step_rejected_when_tool_class_not_in_allowed():
    envelope = ActionEnvelope(
        intended_outcome="send the email",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
    )
    step = _step(tool_class="slack")
    outcome = validate_step_against_envelope(step, envelope)
    assert outcome.valid is False
    assert outcome.reason == REASON_TOOL_CLASS_NOT_ALLOWED
    assert outcome.step_id == step.step_id


def test_step_rejected_when_operation_not_in_allowed():
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=("draft",),
    )
    outcome = validate_step_against_envelope(
        _step(operation_name="send"), envelope
    )
    assert outcome.valid is False
    assert outcome.reason == REASON_OPERATION_NOT_ALLOWED


def test_step_rejected_when_matches_forbidden_move_by_operation():
    """Forbidden_moves is the rejection layer ON TOP OF the allowed
    lists. An operation that's in allowed_operations but also in
    forbidden_moves is still rejected — typical case is "this
    operation is allowed in general but blocked in this context."""
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=("send", "operation_escalation"),
        forbidden_moves=("operation_escalation",),
    )
    outcome = validate_step_against_envelope(
        _step(operation_name="operation_escalation"), envelope
    )
    assert outcome.valid is False
    assert outcome.reason == REASON_FORBIDDEN_MOVE_MATCHED


def test_step_rejected_when_matches_forbidden_move_by_tool_class():
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("slack",),
        allowed_operations=("send",),
        forbidden_moves=("slack",),
    )
    outcome = validate_step_against_envelope(
        _step(tool_class="slack"), envelope
    )
    assert outcome.valid is False
    assert outcome.reason == REASON_FORBIDDEN_MOVE_MATCHED


def test_step_rejected_when_envelope_permits_no_tool_classes_but_step_declares_one():
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=(),
        allowed_operations=("send",),
    )
    outcome = validate_step_against_envelope(_step(), envelope)
    assert outcome.valid is False
    assert outcome.reason == REASON_TOOL_CLASS_NOT_ALLOWED


# ---------------------------------------------------------------------------
# Plan-level rules
# ---------------------------------------------------------------------------


def test_plan_passes_with_valid_steps():
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
    )
    outcome = validate_plan_against_envelope(_plan([_step()]), envelope)
    assert outcome.valid is True


def test_plan_rejected_when_a_step_fails():
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
    )
    plan = _plan([_step(step_id="s1"), _step(step_id="s2", tool_class="slack")])
    outcome = validate_plan_against_envelope(plan, envelope)
    assert outcome.valid is False
    assert outcome.reason == REASON_TOOL_CLASS_NOT_ALLOWED
    assert outcome.step_id == "s2"


def test_plan_rejected_on_confirmation_bypass():
    """A plan that chains a confirmation-required operation in the
    same turn is a structural violation. Confirmation crosses a turn
    boundary; the plan must surface to the user before that step
    can dispatch."""
    envelope = ActionEnvelope(
        intended_outcome="draft and send",
        allowed_tool_classes=("email",),
        allowed_operations=("draft", "send"),
        confirmation_requirements=("send",),
    )
    plan = _plan([
        _step(step_id="s1", operation_name="draft"),
        _step(step_id="s2", operation_name="send"),
    ])
    outcome = validate_plan_against_envelope(plan, envelope)
    assert outcome.valid is False
    assert outcome.reason == REASON_CONFIRMATION_BYPASS
    assert outcome.step_id == "s2"


def test_plan_passes_when_confirmation_required_op_is_absent_from_plan():
    """A draft-only envelope (with send permitted but in confirmation
    requirements) accepts a plan that only drafts. The send happens
    on the next turn after user confirmation, with a fresh envelope."""
    envelope = ActionEnvelope(
        intended_outcome="draft only",
        allowed_tool_classes=("email",),
        allowed_operations=("draft", "send"),
        confirmation_requirements=("send",),
    )
    plan = _plan([_step(step_id="s1", operation_name="draft")])
    outcome = validate_plan_against_envelope(plan, envelope)
    assert outcome.valid is True


def test_plan_rejected_when_empty():
    """Construction of an empty plan already raises PlanValidationError;
    pin via validation function as well for the runtime path that
    might receive an empty plan from a misbehaving planner."""
    envelope = ActionEnvelope(intended_outcome="x")
    # We cannot construct Plan(steps=()) — it raises. Test instead
    # that the validation function detects a zero-step plan when a
    # caller manages to bypass the dataclass. Use object.__new__ to
    # synthesize one.
    plan = object.__new__(Plan)
    object.__setattr__(plan, "plan_id", "p1")
    object.__setattr__(plan, "turn_id", "t1")
    object.__setattr__(plan, "steps", ())
    object.__setattr__(plan, "created_at", "")
    object.__setattr__(plan, "created_via", "initial")
    outcome = validate_plan_against_envelope(plan, envelope)
    assert outcome.valid is False
    assert outcome.reason == REASON_PLAN_EMPTY
