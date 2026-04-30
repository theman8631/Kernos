"""CRBProposalAuthor tests (CRB C3, AC #5, #6, #7, #19, #31).

Pins:

* AC #5: templated scaffold — closing question always present.
* AC #6: low-temperature LLM (temperature <= 0.3).
* AC #7: four authoring methods present with correct signatures.
* AC #19: ambiguity_kind routing — multiple_intents vs.
  modification_target framing.
* AC #31: ProposalAuthor uses templated scaffold + LLM but does NOT
  produce descriptors. (Tested by inspecting LLM prompts for
  descriptor JSON.)
"""
from __future__ import annotations

import inspect

import pytest

from kernos.kernel.cohorts.drafter.signals import CandidateIntent
from kernos.kernel.crb.proposal.author import (
    AmbiguityKind,
    CRBProposalAuthor,
    CapabilityStateSummary,
    LLMTemperatureTooHigh,
    MAX_TEMPERATURE,
)
from kernos.kernel.drafts.registry import WorkflowDraft
from kernos.kernel.substrate_tools import CapabilityGap


class StubLLMClient:
    """Deterministic LLM stub. Records prompts, returns canned text."""

    def __init__(
        self, *, response: str = "", temperature: float = 0.2,
    ) -> None:
        self._temperature = temperature
        self._response = response or "(filled by LLM)? "
        self.prompts: list[str] = []
        self.calls = 0

    @property
    def temperature(self) -> float:
        return self._temperature

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self._response


def _draft(**overrides) -> WorkflowDraft:
    base = dict(
        draft_id="d-1", instance_id="inst_a",
        intent_summary="check email at 9am daily",
        partial_spec_json={
            "triggers": [{"event_type": "schedule.tick"}],
            "action_sequence": [{"action_type": "fetch_email"}],
            "predicate": True,
        },
        display_name="Morning Email Check",
    )
    base.update(overrides)
    return WorkflowDraft(**base)


class _StubWorkflow:
    """Minimal Workflow stub for author_modification_proposal tests."""

    def __init__(self, *, name: str = "previous routine") -> None:
        self.name = name


# ===========================================================================
# AC #6 — low-temperature pin
# ===========================================================================


class TestLowTemperaturePin:
    def test_default_temp_accepted(self):
        # Stub at temp=0.2 is fine.
        author = CRBProposalAuthor(llm_client=StubLLMClient(temperature=0.2))
        assert author.llm_client.temperature <= MAX_TEMPERATURE

    def test_max_temperature_pinned_to_03(self):
        assert MAX_TEMPERATURE == 0.3

    @pytest.mark.parametrize("temp", [0.4, 0.5, 1.0])
    def test_high_temperature_rejected(self, temp):
        with pytest.raises(LLMTemperatureTooHigh):
            CRBProposalAuthor(llm_client=StubLLMClient(temperature=temp))


# ===========================================================================
# AC #7 — four methods present
# ===========================================================================


class TestMethodSurface:
    def test_four_methods_present(self):
        author = CRBProposalAuthor(llm_client=StubLLMClient())
        for name in (
            "author_proposal", "author_gap_message",
            "author_disambiguation", "author_modification_proposal",
        ):
            method = getattr(author, name, None)
            assert callable(method), f"missing method: {name}"
            assert inspect.iscoroutinefunction(method), (
                f"{name} should be a coroutine function"
            )

    def test_author_proposal_signature(self):
        sig = inspect.signature(CRBProposalAuthor.author_proposal)
        params = sig.parameters
        for name in ("draft", "capability_state"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_author_gap_message_signature(self):
        sig = inspect.signature(CRBProposalAuthor.author_gap_message)
        params = sig.parameters
        for name in ("capability_gap", "draft"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_author_disambiguation_signature(self):
        sig = inspect.signature(CRBProposalAuthor.author_disambiguation)
        params = sig.parameters
        for name in ("candidate_intents", "ambiguity_kind"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_author_modification_proposal_signature(self):
        sig = inspect.signature(CRBProposalAuthor.author_modification_proposal)
        params = sig.parameters
        for name in ("draft", "prev_workflow"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY


# ===========================================================================
# AC #5 — templated scaffold; closing question pinned
# ===========================================================================


class TestTemplatedScaffold:
    """Closing question is structural — present even if LLM drops it."""

    async def test_author_proposal_closes_with_question(self):
        # LLM returns text WITHOUT a question mark; defensive fallback
        # appends the templated tail so the closing-question pin holds.
        llm = StubLLMClient(response="Here's the plan")
        author = CRBProposalAuthor(llm_client=llm)
        text = await author.author_proposal(
            draft=_draft(),
            capability_state=CapabilityStateSummary(),
        )
        assert "?" in text

    async def test_author_proposal_with_question_mark_passes_through(self):
        llm = StubLLMClient(
            response="Here's what I'd set up: X. Want me to set this up?",
        )
        author = CRBProposalAuthor(llm_client=llm)
        text = await author.author_proposal(
            draft=_draft(),
            capability_state=CapabilityStateSummary(),
        )
        assert text.endswith("?")

    async def test_author_gap_message_closes_with_question(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        text = await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="sms.send", severity="error",
            ),
            draft=_draft(),
        )
        assert "?" in text

    async def test_author_modification_closes_with_question(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        text = await author.author_modification_proposal(
            draft=_draft(),
            prev_workflow=_StubWorkflow(name="Daily Email Check"),
        )
        assert "?" in text

    async def test_empty_llm_response_falls_back_to_template(self):
        # StubLLMClient default response is non-empty; pass explicit
        # empty + override via complete() patching.
        class _EmptyLLM(StubLLMClient):
            async def complete(self, prompt: str) -> str:
                self.calls += 1
                self.prompts.append(prompt)
                return ""

        llm = _EmptyLLM()
        author = CRBProposalAuthor(llm_client=llm)
        text = await author.author_proposal(
            draft=_draft(),
            capability_state=CapabilityStateSummary(),
        )
        assert "?" in text
        # Fallback contains the literal intent_summary from the draft.
        assert "check email at 9am daily" in text


# ===========================================================================
# AC #19 — ambiguity_kind routing
# ===========================================================================


class TestAmbiguityKindRouting:
    async def test_multiple_intents_framing(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        cands = [
            CandidateIntent(candidate_id="c-1", summary="set up A", confidence=0.8),
            CandidateIntent(candidate_id="c-2", summary="set up B", confidence=0.85),
        ]
        await author.author_disambiguation(
            candidate_intents=cands,
            ambiguity_kind="multiple_intents",
        )
        # The LLM prompt itself contains the multiple-intents template
        # ("a few things").
        assert any("a few things" in p for p in llm.prompts)

    async def test_modification_target_framing(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        cands = [
            CandidateIntent(
                candidate_id="c-1", summary="modify X",
                confidence=0.85, target_workflow_id="wf-x",
            ),
            CandidateIntent(
                candidate_id="c-2", summary="modify Y",
                confidence=0.8, target_workflow_id="wf-y",
            ),
        ]
        await author.author_disambiguation(
            candidate_intents=cands,
            ambiguity_kind="modification_target",
        )
        assert any("a few existing routines" in p for p in llm.prompts)

    async def test_ambiguity_kind_inferred_from_target_workflow_id(self):
        """When ambiguity_kind=None, infer from candidate fields."""
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        cands = [
            CandidateIntent(
                candidate_id="c-1", summary="modify X",
                confidence=0.85, target_workflow_id="wf-x",
            ),
            CandidateIntent(
                candidate_id="c-2", summary="modify Y",
                confidence=0.8, target_workflow_id="wf-y",
            ),
        ]
        await author.author_disambiguation(candidate_intents=cands)
        assert any("a few existing routines" in p for p in llm.prompts)

    async def test_no_target_workflow_id_infers_multiple_intents(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        cands = [
            CandidateIntent(candidate_id="c-1", summary="A", confidence=0.85),
            CandidateIntent(candidate_id="c-2", summary="B", confidence=0.8),
        ]
        await author.author_disambiguation(candidate_intents=cands)
        assert any("a few things" in p for p in llm.prompts)


# ===========================================================================
# AC #31 — no descriptor JSON in LLM prompts
# ===========================================================================


class TestNoDescriptorInPrompts:
    """ProposalAuthor uses templated scaffold + LLM but does NOT produce
    descriptors. The Compiler is the descriptor producer; ProposalAuthor
    is the language layer."""

    async def test_prompts_do_not_contain_descriptor_json(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_proposal(
            draft=_draft(),
            capability_state=CapabilityStateSummary(),
        )
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="sms.send", severity="error",
            ),
            draft=_draft(),
        )
        await author.author_modification_proposal(
            draft=_draft(),
            prev_workflow=_StubWorkflow(),
        )
        # No prompt contains JSON-shaped descriptor markers.
        # ProposalAuthor should never produce or pass full descriptor
        # candidates through the LLM.
        for prompt in llm.prompts:
            # Crude but effective: full descriptor candidates contain
            # "action_sequence", "predicate", "verifier" together.
            has_action_seq = '"action_sequence"' in prompt or "'action_sequence'" in prompt
            has_predicate = '"predicate"' in prompt or "'predicate'" in prompt
            has_verifier = '"verifier"' in prompt or "'verifier'" in prompt
            assert not (has_action_seq and has_predicate and has_verifier), (
                f"ProposalAuthor prompt contains descriptor JSON:\n{prompt}"
            )


# ===========================================================================
# Helper output shape
# ===========================================================================


class TestProviderMapping:
    """Capability tag -> provider name mapping is structural."""

    async def test_sms_send_mentions_twilio(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="sms.send", severity="error",
            ),
            draft=_draft(),
        )
        # Prompt mentions Twilio.
        assert any("Twilio" in p for p in llm.prompts)

    async def test_email_send_mentions_gmail(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="email.send", severity="error",
            ),
            draft=_draft(),
        )
        assert any("Gmail" in p for p in llm.prompts)


# ===========================================================================
# Reproducibility (low-temperature behavior)
# ===========================================================================


class TestReproducibility:
    """Low-temperature LLM should produce structurally similar output
    for the same inputs. Test pin: same draft -> same prompt, so an
    actual low-temp LLM produces similar text."""

    async def test_same_draft_same_prompt(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        draft = _draft()
        cap = CapabilityStateSummary()
        await author.author_proposal(draft=draft, capability_state=cap)
        await author.author_proposal(draft=draft, capability_state=cap)
        # Two calls; both prompts identical.
        assert llm.prompts[0] == llm.prompts[1]
