"""Tests for the trigger predicate AST evaluator.

WORKFLOW-LOOP-PRIMITIVE C2. Predicate evaluation is deterministic
(no LLM, no I/O) and runs on the post-flush hook for every event
batch. These tests pin every operator shape and the
validate-then-evaluate two-stage discipline.
"""
from __future__ import annotations

import pytest

from kernos.kernel.event_stream import Event
from kernos.kernel.workflows.predicates import (
    PredicateError,
    evaluate,
    validate,
)


def _make_event(**overrides) -> Event:
    base = dict(
        event_id="evt-1",
        instance_id="inst_a",
        timestamp="2026-04-28T08:00:00+00:00",
        event_type="cc.batch.report",
        payload={
            "task_id": "T-42",
            "branch": "main",
            "files_changed": 3,
            "tags": ["wlp", "c2"],
            "nested": {"author": "cc"},
        },
        member_id="mem_a",
        space_id="space_main",
        correlation_id="cor-1",
    )
    base.update(overrides)
    return Event(**base)


class TestEqualityOperator:
    def test_eq_payload_field(self):
        ast = {"op": "eq", "path": "payload.task_id", "value": "T-42"}
        validate(ast)
        assert evaluate(ast, _make_event()) is True

    def test_eq_payload_field_miss(self):
        ast = {"op": "eq", "path": "payload.task_id", "value": "T-99"}
        assert evaluate(ast, _make_event()) is False

    def test_eq_top_level_field(self):
        ast = {"op": "eq", "path": "event_type", "value": "cc.batch.report"}
        assert evaluate(ast, _make_event()) is True

    def test_eq_int_value(self):
        ast = {"op": "eq", "path": "payload.files_changed", "value": 3}
        assert evaluate(ast, _make_event()) is True

    def test_eq_missing_path_is_false(self):
        ast = {"op": "eq", "path": "payload.absent", "value": "anything"}
        assert evaluate(ast, _make_event()) is False


class TestContainsOperator:
    def test_contains_substring_match(self):
        ast = {"op": "contains", "path": "payload.task_id", "value": "42"}
        assert evaluate(ast, _make_event()) is True

    def test_contains_non_string_field_is_false(self):
        ast = {"op": "contains", "path": "payload.files_changed", "value": "3"}
        assert evaluate(ast, _make_event()) is False

    def test_contains_missing_field_is_false(self):
        ast = {"op": "contains", "path": "payload.absent", "value": "x"}
        assert evaluate(ast, _make_event()) is False


class TestExistsOperators:
    def test_exists_true(self):
        ast = {"op": "exists", "path": "payload.task_id"}
        assert evaluate(ast, _make_event()) is True

    def test_exists_false(self):
        ast = {"op": "exists", "path": "payload.absent"}
        assert evaluate(ast, _make_event()) is False

    def test_not_exists(self):
        ast = {"op": "not_exists", "path": "payload.absent"}
        assert evaluate(ast, _make_event()) is True

    def test_exists_on_nested(self):
        ast = {"op": "exists", "path": "payload.nested.author"}
        assert evaluate(ast, _make_event()) is True


class TestInSetOperator:
    def test_in_set_hit(self):
        ast = {"op": "in_set", "path": "payload.task_id",
               "values": ["T-1", "T-42", "T-99"]}
        assert evaluate(ast, _make_event()) is True

    def test_in_set_miss(self):
        ast = {"op": "in_set", "path": "payload.task_id", "values": ["T-1"]}
        assert evaluate(ast, _make_event()) is False

    def test_in_set_missing_path_is_false(self):
        ast = {"op": "in_set", "path": "payload.absent", "values": ["x"]}
        assert evaluate(ast, _make_event()) is False


class TestTimeWindow:
    def test_within_window(self):
        ast = {"op": "time_window",
               "start": "2026-04-28T00:00:00+00:00",
               "end": "2026-04-28T23:59:59+00:00"}
        assert evaluate(ast, _make_event()) is True

    def test_before_window(self):
        ast = {"op": "time_window",
               "start": "2026-04-29T00:00:00+00:00",
               "end": "2026-04-29T23:59:59+00:00"}
        assert evaluate(ast, _make_event()) is False


class TestActorAndCorrelation:
    def test_actor_eq_match(self):
        ast = {"op": "actor_eq", "value": "mem_a"}
        assert evaluate(ast, _make_event()) is True

    def test_actor_eq_miss(self):
        ast = {"op": "actor_eq", "value": "mem_b"}
        assert evaluate(ast, _make_event()) is False

    def test_correlation_eq_match(self):
        ast = {"op": "correlation_eq", "value": "cor-1"}
        assert evaluate(ast, _make_event()) is True


class TestEventTypePrefix:
    def test_prefix_match(self):
        ast = {"op": "event_type_starts_with", "prefix": "cc.batch."}
        assert evaluate(ast, _make_event()) is True

    def test_prefix_miss(self):
        ast = {"op": "event_type_starts_with", "prefix": "tool."}
        assert evaluate(ast, _make_event()) is False


class TestComposites:
    def test_and_all_true(self):
        ast = {"op": "AND", "operands": [
            {"op": "eq", "path": "event_type", "value": "cc.batch.report"},
            {"op": "actor_eq", "value": "mem_a"},
        ]}
        assert evaluate(ast, _make_event()) is True

    def test_and_one_false(self):
        ast = {"op": "AND", "operands": [
            {"op": "eq", "path": "event_type", "value": "cc.batch.report"},
            {"op": "actor_eq", "value": "mem_other"},
        ]}
        assert evaluate(ast, _make_event()) is False

    def test_or_one_true(self):
        ast = {"op": "OR", "operands": [
            {"op": "actor_eq", "value": "mem_other"},
            {"op": "actor_eq", "value": "mem_a"},
        ]}
        assert evaluate(ast, _make_event()) is True

    def test_or_all_false(self):
        ast = {"op": "OR", "operands": [
            {"op": "actor_eq", "value": "mem_other"},
            {"op": "eq", "path": "payload.task_id", "value": "T-99"},
        ]}
        assert evaluate(ast, _make_event()) is False

    def test_not(self):
        ast = {"op": "NOT", "operand": {"op": "actor_eq", "value": "mem_other"}}
        assert evaluate(ast, _make_event()) is True

    def test_nested_composition(self):
        ast = {
            "op": "AND",
            "operands": [
                {"op": "event_type_starts_with", "prefix": "cc."},
                {"op": "OR", "operands": [
                    {"op": "actor_eq", "value": "mem_a"},
                    {"op": "actor_eq", "value": "mem_b"},
                ]},
                {"op": "NOT", "operand": {
                    "op": "eq", "path": "payload.task_id", "value": "T-skip",
                }},
            ],
        }
        validate(ast)
        assert evaluate(ast, _make_event()) is True


class TestDeterminism:
    def test_same_event_same_result(self):
        """Two evaluations of the same predicate against the same
        event must return the same result (no hidden randomness or
        I/O)."""
        ast = {"op": "AND", "operands": [
            {"op": "eq", "path": "event_type", "value": "cc.batch.report"},
            {"op": "actor_eq", "value": "mem_a"},
            {"op": "in_set", "path": "payload.task_id",
             "values": ["T-42", "T-7"]},
        ]}
        validate(ast)
        e = _make_event()
        results = [evaluate(ast, e) for _ in range(20)]
        assert all(r is True for r in results)


class TestValidate:
    def test_unknown_op_rejected(self):
        with pytest.raises(PredicateError):
            validate({"op": "wat", "path": "x", "value": 1})

    def test_eq_missing_value_rejected(self):
        with pytest.raises(PredicateError):
            validate({"op": "eq", "path": "x"})

    def test_eq_missing_path_rejected(self):
        with pytest.raises(PredicateError):
            validate({"op": "eq", "value": 1})

    def test_in_set_requires_list(self):
        with pytest.raises(PredicateError):
            validate({"op": "in_set", "path": "x", "values": "not-a-list"})

    def test_time_window_requires_strings(self):
        with pytest.raises(PredicateError):
            validate({"op": "time_window", "start": 1, "end": 2})

    def test_and_empty_operands_rejected(self):
        with pytest.raises(PredicateError):
            validate({"op": "AND", "operands": []})

    def test_not_missing_operand_rejected(self):
        with pytest.raises(PredicateError):
            validate({"op": "NOT"})

    def test_event_type_starts_with_requires_prefix(self):
        with pytest.raises(PredicateError):
            validate({"op": "event_type_starts_with", "prefix": ""})

    def test_validate_recurses_into_composites(self):
        with pytest.raises(PredicateError):
            validate({"op": "AND", "operands": [
                {"op": "eq", "path": "x", "value": 1},
                {"op": "wat", "path": "y", "value": 2},
            ]})

    def test_top_level_must_be_dict(self):
        with pytest.raises(PredicateError):
            validate(["op"])
