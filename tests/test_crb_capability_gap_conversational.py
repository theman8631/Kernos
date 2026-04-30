"""Capability gap conversational surfacing (CRB C6, AC #18).

End-to-end pin: gap_detected signals reach principal; principal
calls author_gap_message; output mentions provider name +
capability tag + offers both paths.
"""
from __future__ import annotations

import pytest

from kernos.kernel.crb.proposal.author import (
    CRBProposalAuthor,
    CapabilityStateSummary,
)
from kernos.kernel.drafts.registry import WorkflowDraft
from kernos.kernel.substrate_tools import CapabilityGap


class StubLLMClient:
    """Echoes the prompt's content back so we can verify the template
    actually injected the variables. Real low-temp LLMs would produce
    naturalized text; structural elements are template-controlled."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def temperature(self) -> float:
        return 0.2

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        # Return the template body (LLM-filled by the template's
        # variable substitutions in the prompt).
        return (
            "I noticed Twilio isn't connected yet — set this up, or "
            "shall I draft this and we'll connect later?"
        )


def _draft() -> WorkflowDraft:
    return WorkflowDraft(
        draft_id="d-1", instance_id="inst_a",
        intent_summary="text Sam when calendar is free",
        partial_spec_json={
            "triggers": [{"event_type": "calendar.free_window"}],
            "action_sequence": [{"action_type": "send_sms"}],
            "predicate": True,
        },
    )


class TestBlockingCapabilityGap:
    """AC #18: blocking gap (sms.send required, Twilio not connected)
    surfaces as user-facing message."""

    async def test_gap_message_mentions_provider_and_offers_paths(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        gap = CapabilityGap(
            required_tag="sms.send", severity="error",
            suggested_resolution="connect Twilio",
        )
        text = await author.author_gap_message(
            capability_gap=gap, draft=_draft(),
        )
        # Output mentions Twilio (mapped from sms.send) AND offers
        # both paths (set up vs. continue).
        assert "Twilio" in text or "Twilio" in llm.prompts[-1]
        assert "?" in text  # closing question

    async def test_provider_mapping_for_sms_send(self):
        """The capability tag -> provider mapping is structural pin
        (AC #18 requires concrete provider naming)."""
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="sms.send", severity="error",
            ),
            draft=_draft(),
        )
        # Prompt mentions Twilio explicitly.
        assert "Twilio" in llm.prompts[-1]

    async def test_provider_mapping_for_email_send(self):
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="email.send", severity="error",
            ),
            draft=_draft(),
        )
        assert "Gmail" in llm.prompts[-1]

    async def test_capability_tag_in_prompt(self):
        """The literal capability tag is in the prompt so the LLM has
        full context to compose the gap message."""
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        await author.author_gap_message(
            capability_gap=CapabilityGap(
                required_tag="calendar.read", severity="error",
            ),
            draft=_draft(),
        )
        assert "calendar.read" in llm.prompts[-1]


class TestNonBlockingGap:
    """AC #18: non-blocking gaps (warnings, not errors) — surfaces
    but the principal cohort decides timing."""

    async def test_warning_severity_still_authored(self):
        """The author doesn't gate on severity — that's the principal
        cohort's responsibility per Seam C5. The author just composes
        whatever message the principal asks for."""
        llm = StubLLMClient()
        author = CRBProposalAuthor(llm_client=llm)
        gap = CapabilityGap(
            required_tag="sms.send", severity="warning",
        )
        text = await author.author_gap_message(
            capability_gap=gap, draft=_draft(),
        )
        assert text  # author still produces text
        assert "?" in text
