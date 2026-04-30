"""CRB Compiler shape-assertion tests (CRB C1, AC #2, #3).

Pins:

* Required fields present (intent_summary, triggers, action_sequence,
  predicate). Missing field -> DraftSchemaIncomplete.
* Triggers list non-empty + each trigger dict with event_type.
* Action sequence list non-empty + each action dict with action_type.
* Predicate AST permits booleans, "true"/"false", and dicts with
  recognized op. Invalid -> DraftShapeMalformed.
* Capability / provider validation NOT done by Compiler — passes
  drafts referencing missing capabilities to STS dry-run.
"""
from __future__ import annotations

import pytest

from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.errors import (
    DraftSchemaIncomplete,
    DraftShapeMalformed,
)
from kernos.kernel.drafts.registry import WorkflowDraft


def _draft(**overrides) -> WorkflowDraft:
    base = dict(
        draft_id="d-1",
        instance_id="inst_a",
        intent_summary="test routine",
        partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
        },
    )
    base.update(overrides)
    return WorkflowDraft(**base)


class TestRequiredFields:
    def test_missing_intent_summary_raises_schema_incomplete(self):
        # WorkflowDraft has empty default; clear it on the draft.
        draft = _draft(intent_summary="")
        with pytest.raises(DraftSchemaIncomplete, match="intent_summary"):
            draft_to_descriptor_candidate(draft)

    def test_missing_instance_id_raises(self):
        draft = _draft(instance_id="")
        with pytest.raises(DraftSchemaIncomplete, match="instance_id"):
            draft_to_descriptor_candidate(draft)

    def test_missing_triggers_raises(self):
        draft = _draft(partial_spec_json={
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
        })
        with pytest.raises(DraftSchemaIncomplete, match="triggers"):
            draft_to_descriptor_candidate(draft)

    def test_missing_action_sequence_raises(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "predicate": True,
        })
        with pytest.raises(DraftSchemaIncomplete, match="action_sequence"):
            draft_to_descriptor_candidate(draft)

    def test_missing_predicate_raises(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
        })
        with pytest.raises(DraftSchemaIncomplete, match="predicate"):
            draft_to_descriptor_candidate(draft)


class TestTriggerShapeAssertions:
    def test_triggers_must_be_list(self):
        draft = _draft(partial_spec_json={
            "triggers": "not a list",
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
        })
        with pytest.raises(DraftShapeMalformed, match="triggers must be a list"):
            draft_to_descriptor_candidate(draft)

    def test_triggers_must_be_non_empty(self):
        draft = _draft(partial_spec_json={
            "triggers": [],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
        })
        with pytest.raises(DraftShapeMalformed, match="non-empty"):
            draft_to_descriptor_candidate(draft)

    def test_trigger_must_have_event_type(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"no_event_type": "oops"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
        })
        with pytest.raises(DraftShapeMalformed, match="event_type"):
            draft_to_descriptor_candidate(draft)


class TestActionSequenceShapeAssertions:
    def test_action_sequence_must_be_non_empty(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [],
            "predicate": True,
        })
        with pytest.raises(DraftShapeMalformed, match="non-empty"):
            draft_to_descriptor_candidate(draft)

    def test_action_must_have_action_type(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"no_action_type": "oops"}],
            "predicate": True,
        })
        with pytest.raises(DraftShapeMalformed, match="action_type"):
            draft_to_descriptor_candidate(draft)


class TestPredicateAstShape:
    def test_bool_literal_accepted(self):
        for predicate in (True, False, "true", "false"):
            draft = _draft(partial_spec_json={
                "triggers": [{"event_type": "tool.called"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": predicate,
            })
            # No raise.
            draft_to_descriptor_candidate(draft)

    def test_recognized_op_accepted(self):
        for op in ("eq", "and", "or", "not", "in", "exists"):
            if op == "and" or op == "or":
                pred = {"op": op, "operands": [True, False]}
            elif op == "not":
                pred = {"op": op, "operand": True}
            else:
                pred = {"op": op}
            draft = _draft(partial_spec_json={
                "triggers": [{"event_type": "tool.called"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": pred,
            })
            draft_to_descriptor_candidate(draft)

    def test_unrecognized_op_raises(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": {"op": "bogus_op"},
        })
        with pytest.raises(DraftShapeMalformed, match="bogus_op"):
            draft_to_descriptor_candidate(draft)

    def test_predicate_dict_missing_op_raises(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": {"no_op_field": "x"},
        })
        with pytest.raises(DraftShapeMalformed, match="missing required field 'op'"):
            draft_to_descriptor_candidate(draft)

    def test_recursive_and_validates_operands(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": {"op": "and", "operands": [True, {"op": "bogus"}]},
        })
        with pytest.raises(DraftShapeMalformed, match="bogus"):
            draft_to_descriptor_candidate(draft)


class TestBoundsShape:
    def test_negative_bounds_rejected(self):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [{"action_type": "mark_state"}],
            "predicate": True,
            "bounds": {"iteration_count": -1},
        })
        with pytest.raises(DraftShapeMalformed, match="non-negative"):
            draft_to_descriptor_candidate(draft)


class TestCapabilityValidationDeferredToSTS:
    """AC #3: Compiler doesn't reject drafts referencing missing
    capabilities/providers. STS dry-run is the validator for that."""

    def test_unknown_provider_passes_compiler(self):
        # Reference a non-existent provider in an action.
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [
                {
                    "action_type": "route_to_agent",
                    "parameters": {
                        "agent_id": "nonexistent-agent",
                        "envelope": {},
                    },
                },
            ],
            "predicate": True,
        })
        # Compiler doesn't validate agent existence — STS does.
        # No raise from Compiler.
        result = draft_to_descriptor_candidate(draft)
        assert result is not None
        assert result["action_sequence"][0]["parameters"]["agent_id"] == "nonexistent-agent"
