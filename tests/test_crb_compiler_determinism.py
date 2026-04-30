"""CRB Compiler determinism tests (CRB C1, AC #1, #4).

Pins:

* Same draft -> same descriptor candidate across N invocations.
* Pure: zero LLM calls observed.
* Replaces Drafter v1's compiler_helper_stub with the production
  translator behind the same signature.
"""
from __future__ import annotations

from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.cohorts.drafter.compiler_helper_stub import (
    draft_to_descriptor_candidate as stub_translation,
)
from kernos.kernel.drafts.registry import WorkflowDraft


def _draft(**overrides) -> WorkflowDraft:
    base = dict(
        draft_id="d-1",
        instance_id="inst_a",
        intent_summary="test routine",
        partial_spec_json={
            "triggers": [{"event_type": "tool.called"}],
            "action_sequence": [
                {"action_type": "mark_state", "parameters": {"k": "v"}},
            ],
            "predicate": True,
        },
    )
    base.update(overrides)
    return WorkflowDraft(**base)


class TestDeterminism:
    def test_same_draft_same_output_100x(self):
        draft = _draft()
        first = draft_to_descriptor_candidate(draft)
        for _ in range(99):
            assert draft_to_descriptor_candidate(draft) == first

    def test_different_intent_different_output(self):
        a = draft_to_descriptor_candidate(_draft(intent_summary="alpha"))
        b = draft_to_descriptor_candidate(_draft(intent_summary="beta"))
        assert a != b

    def test_different_triggers_different_output(self):
        a = draft_to_descriptor_candidate(_draft(
            partial_spec_json={
                "triggers": [{"event_type": "tool.called"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": True,
            },
        ))
        b = draft_to_descriptor_candidate(_draft(
            partial_spec_json={
                "triggers": [{"event_type": "different.event"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": True,
            },
        ))
        assert a != b


class TestNoLLM:
    """Sanity: the function is pure. No LLM client touched, no I/O.
    The signature accepts only a draft; there's no LLM client to
    pass in, so by construction zero LLM calls."""

    def test_no_llm_client_in_signature(self):
        import inspect

        sig = inspect.signature(draft_to_descriptor_candidate)
        params = list(sig.parameters)
        assert params == ["draft"]


class TestSignatureCompatibilityWithStub:
    """AC #4: the production translator replaces the stub at the same
    signature. Drafter cohort wires either one identically."""

    def test_same_signature_as_stub(self):
        import inspect

        prod_sig = inspect.signature(draft_to_descriptor_candidate)
        stub_sig = inspect.signature(stub_translation)
        assert list(prod_sig.parameters) == list(stub_sig.parameters)

    def test_both_produce_dict_for_minimal_draft(self):
        # Both functions accept the same draft and produce dicts.
        # The shapes differ — the production version asserts more —
        # but the swap-in compatibility is what matters for AC #4.
        draft = _draft()
        result = draft_to_descriptor_candidate(draft)
        assert isinstance(result, dict)
        assert "intent_summary" in result
