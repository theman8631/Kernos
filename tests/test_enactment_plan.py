"""Tests for the Plan / Step / StepExpectation / StructuredSignal
vocabulary (PDI C5).

The vocabulary is permanent for the life of the system. These tests
pin every signal kind and every dataclass-validation rule so future
changes to the seven canonical signal types fail loudly. Adding an
eighth kind is a coordinated migration; tightening or relaxing one
of the existing kinds is a contract break.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.plan import (
    Plan,
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    evaluate_expectation_signals,
    evaluate_signal,
    new_plan_id,
    now_iso,
)


# ---------------------------------------------------------------------------
# StructuredSignal — closed enum + round-trip
# ---------------------------------------------------------------------------


def test_signal_kinds_are_exactly_seven():
    """Vocabulary lock: exactly seven signal kinds. Adding an eighth
    is a coordinated migration; this pin makes that explicit."""
    expected = {
        "count_at_least",
        "count_at_most",
        "contains_field",
        "returns_truthy",
        "success_status",
        "value_equality",
        "value_in_set",
    }
    assert {k.value for k in SignalKind} == expected


def test_signal_round_trip_via_dict():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_LEAST,
        args={"path": "results", "value": 3},
    )
    parsed = StructuredSignal.from_dict(sig.to_dict())
    assert parsed == sig


def test_signal_rejects_unknown_kind_on_from_dict():
    with pytest.raises(PlanValidationError, match="not one of"):
        StructuredSignal.from_dict({"kind": "frobnicate", "args": {}})


def test_signal_rejects_non_dict_args():
    with pytest.raises(PlanValidationError, match="args must be a dict"):
        StructuredSignal(kind=SignalKind.RETURNS_TRUTHY, args="not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Signal evaluation — each kind
# ---------------------------------------------------------------------------


def test_count_at_least_passes():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_LEAST,
        args={"path": "results", "value": 2},
    )
    assert evaluate_signal(sig, {"results": [1, 2, 3]}) is True


def test_count_at_least_fails_when_short():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_LEAST,
        args={"path": "results", "value": 5},
    )
    assert evaluate_signal(sig, {"results": [1, 2, 3]}) is False


def test_count_at_least_fails_when_path_missing():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_LEAST,
        args={"path": "missing", "value": 1},
    )
    assert evaluate_signal(sig, {"other": []}) is False


def test_count_at_most_passes():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_MOST,
        args={"path": "items", "value": 10},
    )
    assert evaluate_signal(sig, {"items": [1, 2]}) is True


def test_count_at_most_fails_when_too_many():
    sig = StructuredSignal(
        kind=SignalKind.COUNT_AT_MOST,
        args={"path": "items", "value": 1},
    )
    assert evaluate_signal(sig, {"items": [1, 2]}) is False


def test_contains_field_passes():
    sig = StructuredSignal(
        kind=SignalKind.CONTAINS_FIELD,
        args={"path": "user", "key": "address"},
    )
    assert evaluate_signal(sig, {"user": {"address": "x"}}) is True


def test_contains_field_fails_when_key_missing():
    sig = StructuredSignal(
        kind=SignalKind.CONTAINS_FIELD,
        args={"path": "user", "key": "ssn"},
    )
    assert evaluate_signal(sig, {"user": {"address": "x"}}) is False


def test_contains_field_fails_when_path_is_not_dict():
    sig = StructuredSignal(
        kind=SignalKind.CONTAINS_FIELD,
        args={"path": "items", "key": "anything"},
    )
    assert evaluate_signal(sig, {"items": [1, 2]}) is False


def test_returns_truthy_passes_for_non_empty_list():
    sig = StructuredSignal(kind=SignalKind.RETURNS_TRUTHY, args={"path": "results"})
    assert evaluate_signal(sig, {"results": [1]}) is True


def test_returns_truthy_fails_for_empty_list():
    sig = StructuredSignal(kind=SignalKind.RETURNS_TRUTHY, args={"path": "results"})
    assert evaluate_signal(sig, {"results": []}) is False


def test_returns_truthy_at_root():
    sig = StructuredSignal(kind=SignalKind.RETURNS_TRUTHY, args={})
    assert evaluate_signal(sig, {"any": "value"}) is True
    assert evaluate_signal(sig, {}) is False


def test_success_status_default_path_ok_value_true():
    sig = StructuredSignal(kind=SignalKind.SUCCESS_STATUS, args={})
    assert evaluate_signal(sig, {"ok": True}) is True
    assert evaluate_signal(sig, {"ok": False}) is False


def test_success_status_custom_path_and_value():
    sig = StructuredSignal(
        kind=SignalKind.SUCCESS_STATUS,
        args={"path": "status", "value": "completed"},
    )
    assert evaluate_signal(sig, {"status": "completed"}) is True
    assert evaluate_signal(sig, {"status": "pending"}) is False


def test_value_equality_passes():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_EQUALITY,
        args={"path": "type", "value": "invoice"},
    )
    assert evaluate_signal(sig, {"type": "invoice"}) is True


def test_value_equality_fails_on_mismatch():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_EQUALITY,
        args={"path": "type", "value": "invoice"},
    )
    assert evaluate_signal(sig, {"type": "receipt"}) is False


def test_value_in_set_passes():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_IN_SET,
        args={"path": "status_code", "values": [200, 201, 204]},
    )
    assert evaluate_signal(sig, {"status_code": 201}) is True


def test_value_in_set_fails_when_outside():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_IN_SET,
        args={"path": "status_code", "values": [200, 201]},
    )
    assert evaluate_signal(sig, {"status_code": 500}) is False


def test_nested_path_resolution():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_EQUALITY,
        args={"path": "user.profile.locale", "value": "en-US"},
    )
    assert evaluate_signal(
        sig, {"user": {"profile": {"locale": "en-US"}}}
    ) is True


def test_nested_path_failure_when_intermediate_missing():
    sig = StructuredSignal(
        kind=SignalKind.VALUE_EQUALITY,
        args={"path": "user.profile.locale", "value": "en-US"},
    )
    assert evaluate_signal(sig, {"user": {}}) is False


# ---------------------------------------------------------------------------
# evaluate_expectation_signals — the runtime entry point
# ---------------------------------------------------------------------------


def test_evaluate_expectation_with_no_structured_signals_returns_pass():
    """No structured signals → returns (True, ()). The runtime then
    falls to model-judged prose comparison."""
    expectation = StepExpectation(prose="anything reasonable")
    passed, failures = evaluate_expectation_signals(expectation, {"any": "result"})
    assert passed is True
    assert failures == ()


def test_evaluate_expectation_collects_all_failures():
    expectation = StepExpectation(
        prose="must succeed and return at least one result",
        structured=(
            StructuredSignal(kind=SignalKind.SUCCESS_STATUS, args={}),
            StructuredSignal(
                kind=SignalKind.COUNT_AT_LEAST,
                args={"path": "results", "value": 1},
            ),
        ),
    )
    # Both signals fail.
    passed, failures = evaluate_expectation_signals(
        expectation, {"ok": False, "results": []}
    )
    assert passed is False
    assert len(failures) == 2


def test_evaluate_expectation_partial_failure():
    expectation = StepExpectation(
        prose="x",
        structured=(
            StructuredSignal(kind=SignalKind.SUCCESS_STATUS, args={}),
            StructuredSignal(
                kind=SignalKind.COUNT_AT_LEAST,
                args={"path": "results", "value": 1},
            ),
        ),
    )
    passed, failures = evaluate_expectation_signals(
        expectation, {"ok": True, "results": []}
    )
    assert passed is False
    assert len(failures) == 1
    assert failures[0].kind is SignalKind.COUNT_AT_LEAST


# ---------------------------------------------------------------------------
# StepExpectation construction validation
# ---------------------------------------------------------------------------


def test_expectation_requires_non_empty_prose():
    with pytest.raises(PlanValidationError, match="prose"):
        StepExpectation(prose="   ")


def test_expectation_round_trip():
    e = StepExpectation(
        prose="email sent successfully",
        structured=(
            StructuredSignal(kind=SignalKind.SUCCESS_STATUS, args={}),
        ),
    )
    parsed = StepExpectation.from_dict(e.to_dict())
    assert parsed == e


# ---------------------------------------------------------------------------
# Step + Plan construction validation
# ---------------------------------------------------------------------------


def _expectation() -> StepExpectation:
    return StepExpectation(prose="x")


def _step(step_id: str = "s1", **overrides) -> Step:
    base = dict(
        step_id=step_id,
        tool_id="email_send",
        arguments={"to": "x@example.com"},
        tool_class="email",
        operation_name="send",
        expectation=_expectation(),
    )
    base.update(overrides)
    return Step(**base)


def test_step_requires_non_empty_tool_id():
    with pytest.raises(PlanValidationError, match="tool_id"):
        _step(tool_id="")


def test_step_requires_step_id():
    with pytest.raises(PlanValidationError, match="step_id"):
        _step(step_id="")


def test_step_round_trip():
    s = _step()
    assert Step.from_dict(s.to_dict()) == s


def test_plan_requires_at_least_one_step():
    with pytest.raises(PlanValidationError, match="non-empty"):
        Plan(plan_id="p1", turn_id="t1", steps=())


def test_plan_round_trip():
    p = Plan(
        plan_id=new_plan_id(),
        turn_id="turn-1",
        steps=(_step("s1"), _step("s2")),
        created_at=now_iso(),
    )
    assert Plan.from_dict(p.to_dict()) == p


def test_plan_default_created_via_is_initial():
    p = Plan(plan_id="p1", turn_id="t1", steps=(_step(),))
    assert p.created_via == "initial"


def test_plan_id_is_unique_per_call():
    a = new_plan_id()
    b = new_plan_id()
    assert a != b
