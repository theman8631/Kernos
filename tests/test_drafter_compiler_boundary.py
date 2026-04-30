"""Compiler-helper boundary tests (DRAFTER C3, AC #21)."""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.compiler_helper_stub import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.drafts.registry import WorkflowDraft


def _draft(**overrides) -> WorkflowDraft:
    base = dict(
        draft_id="d-1",
        instance_id="inst_a",
        intent_summary="test routine",
        partial_spec_json={"name": "spec_name", "version": "1"},
    )
    base.update(overrides)
    return WorkflowDraft(**base)


class TestStubDeterminism:
    def test_same_input_same_output(self):
        d = _draft()
        a = draft_to_descriptor_candidate(d)
        b = draft_to_descriptor_candidate(d)
        assert a == b

    def test_different_partial_spec_different_output(self):
        a = draft_to_descriptor_candidate(_draft(
            partial_spec_json={"name": "a"},
        ))
        b = draft_to_descriptor_candidate(_draft(
            partial_spec_json={"name": "b"},
        ))
        assert a != b


class TestStubPassThrough:
    def test_partial_spec_keys_preserved(self):
        result = draft_to_descriptor_candidate(_draft(
            partial_spec_json={
                "name": "spec_name",
                "verifier": {"flavor": "deterministic", "check": "x == y"},
                "trigger": {"event_type": "tool.called"},
            },
        ))
        assert result["name"] == "spec_name"
        assert result["verifier"] == {"flavor": "deterministic", "check": "x == y"}
        assert result["trigger"] == {"event_type": "tool.called"}

    def test_instance_id_filled_from_draft(self):
        result = draft_to_descriptor_candidate(_draft(
            instance_id="inst_a",
            partial_spec_json={},
        ))
        assert result["instance_id"] == "inst_a"

    def test_intent_summary_passed_through(self):
        result = draft_to_descriptor_candidate(_draft(
            intent_summary="custom intent",
            partial_spec_json={},
        ))
        assert result["intent_summary"] == "custom intent"


class TestNoneInputRejected:
    def test_none_draft_raises(self):
        with pytest.raises(ValueError):
            draft_to_descriptor_candidate(None)  # type: ignore[arg-type]
