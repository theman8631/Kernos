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


class TestDeepCopyIsolation:
    """Codex mid-batch hardening: spec-derived fields are deepcopied
    so a downstream consumer mutating the returned candidate cannot
    contaminate the draft's partial_spec_json."""

    def test_caller_mutation_does_not_affect_draft(self):
        draft = _draft()
        a = draft_to_descriptor_candidate(draft)
        # Mutate the returned candidate's nested structures.
        a["action_sequence"].append({"action_type": "injected"})
        a["triggers"][0]["event_type"] = "mutated"
        # The draft's partial_spec_json must be unaffected.
        assert (
            len(draft.partial_spec_json["action_sequence"]) == 1
        ), "caller mutation contaminated draft.action_sequence"
        assert (
            draft.partial_spec_json["triggers"][0]["event_type"]
            == "tool.called"
        ), "caller mutation contaminated draft.triggers"

    def test_subsequent_translation_unaffected_by_prior_mutation(self):
        draft = _draft()
        first = draft_to_descriptor_candidate(draft)
        first["action_sequence"].append({"action_type": "injected"})
        # A second translation on the same draft must produce the
        # original (un-injected) shape.
        second = draft_to_descriptor_candidate(draft)
        assert len(second["action_sequence"]) == 1


class TestMetadataIntentSummaryAuthoritative:
    """Codex hardening: metadata.intent_summary is forced from
    draft.intent_summary (NOT setdefault) so a stale partial_spec_json
    metadata cannot diverge from the draft's authoritative summary."""

    def test_authoritative_intent_summary(self):
        draft = _draft(
            intent_summary="canonical summary",
            partial_spec_json={
                "triggers": [{"event_type": "tool.called"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": True,
                "metadata": {"intent_summary": "stale stale stale"},
            },
        )
        result = draft_to_descriptor_candidate(draft)
        assert result["metadata"]["intent_summary"] == "canonical summary"


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
