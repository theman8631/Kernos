"""CRBProposalAuthor — LLM-driven user-facing language layer.

Authors text for InstallProposal, gap messages, disambiguation
questions, and modification proposals. Low-temperature LLM calls with
templated scaffold structure — the structural elements are template-
controlled so output is predictable; the variable parts (intent
summary, capability names, candidate descriptions) are LLM-filled.

**Elegance latitude (deviation from spec API).** Spec described
``author_proposal`` and ``author_modification_proposal`` returning
``InstallProposal``, but the constructor was specified without a
store dependency. Returning a persisted ``InstallProposal`` from a
stateless author is contradictory. C3 ships the methods returning
``str`` (just the user-facing text); CRBApprovalFlow (C4) takes the
text and persists via :meth:`InstallProposalStore.create_proposal`.
This keeps ProposalAuthor pure and stateless, decouples authoring
from persistence, and matches the way ApprovalFlow needs to
correlate proposal_id / correlation_id / etc anyway.

Templated scaffolds (per spec section "ProposalAuthor templated
scaffold") use simple format strings with LLM-filled variable parts.
The templates are inline in this module — for v1 a separate
TemplateRegistry would add complexity without concrete benefit.

Anti-fragmentation invariant: ProposalAuthor consumes existing
context surfaces (draft, capability_state passed in by caller); does
NOT build a parallel context model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol, TYPE_CHECKING

from kernos.kernel.crb.errors import CRBError

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.cohorts.drafter.signals import CandidateIntent
    from kernos.kernel.drafts.registry import WorkflowDraft
    from kernos.kernel.substrate_tools import CapabilityGap
    from kernos.kernel.workflows.workflow_registry import Workflow


# ---------------------------------------------------------------------------
# LLM contract
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """Async low-temperature text-completion client.

    Drafter / CRB / future cohorts share the same shape so tests can
    swap in deterministic stubs. ``temperature`` is the v1 invariant
    pin (low-temp); ``complete`` returns the raw text response.
    """

    @property
    def temperature(self) -> float:
        ...

    async def complete(self, prompt: str) -> str:
        ...


# Maximum permitted temperature for ProposalAuthor's LLM (AC #6).
MAX_TEMPERATURE = 0.3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProposalAuthoringError(CRBError):
    """Base for ProposalAuthor errors."""


class TemplateRegistryEmpty(ProposalAuthoringError):
    """Required template missing. Configuration bug."""


class LLMResponseMalformed(ProposalAuthoringError):
    """LLM returned a response that didn't fit the templated scaffold.
    Retry once; on second failure surface to operator diagnostic."""


class LLMTemperatureTooHigh(ProposalAuthoringError):
    """LLM client configured with temperature above MAX_TEMPERATURE.
    Pin (AC #6): authoring uses low-temperature output for
    reproducibility."""


# ---------------------------------------------------------------------------
# Capability state shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityStateSummary:
    """Shape ProposalAuthor expects for capability state context.

    Caller (ApprovalFlow / principal cohort) builds this from STS
    ``list_known_providers`` + draft requirements. Pure value type;
    no I/O.
    """

    connected_capability_tags: tuple[str, ...] = ()
    missing_capability_tags: tuple[str, ...] = ()


# Type alias for the disambiguation kind.
AmbiguityKind = Literal["multiple_intents", "modification_target"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


_PROPOSAL_TEMPLATE = (
    "Here's what I'd set up: {intent_summary}. "
    "Trigger: {trigger_summary}. "
    "Action: {action_summary}. "
    "Want me to set this up?"
)

_GAP_TEMPLATE = (
    "I noticed {provider_name} isn't connected yet — "
    "{intent_summary} needs {capability_tag}. "
    "Want to set it up, or shall I draft this and we'll connect later?"
)

_DISAMBIGUATION_MULTIPLE_TEMPLATE = (
    "You mentioned a few things — {candidate_summaries}. "
    "Which would you like me to track?"
)

_DISAMBIGUATION_MODIFICATION_TEMPLATE = (
    "I see a few existing routines that could match — "
    "{candidate_summaries}. Which one did you mean?"
)

_MODIFICATION_TEMPLATE = (
    "I'd update the {routine_name} routine — {diff_summary}. "
    "Want me to apply this change?"
)


# Closing-question pin (AC #5): every proposal/modification template
# ends with a question mark + offers user choice. Tested via
# structural assertion.
_CLOSING_QUESTION_MARKERS: tuple[str, ...] = (
    "?",
)


# ---------------------------------------------------------------------------
# Author
# ---------------------------------------------------------------------------


class CRBProposalAuthor:
    """LLM-driven user-facing language layer.

    Anti-fragmentation invariant: consumes existing context surfaces;
    does NOT build a parallel context model. Future-composition
    invariant: when shared situation model lands post-CRB,
    ProposalAuthor slots in by consuming the new shared object
    without internal rework.
    """

    def __init__(self, *, llm_client: LLMClient) -> None:
        # AC #6 pin: low-temperature.
        if llm_client.temperature > MAX_TEMPERATURE:
            raise LLMTemperatureTooHigh(
                f"ProposalAuthor LLM client temperature="
                f"{llm_client.temperature}; must be <= "
                f"{MAX_TEMPERATURE} for low-temperature authoring "
                f"(AC #6 pin)"
            )
        self._llm = llm_client

    @property
    def llm_client(self) -> LLMClient:
        return self._llm

    # -- author_proposal -----------------------------------------------

    async def author_proposal(
        self,
        *,
        draft: "WorkflowDraft",
        capability_state: CapabilityStateSummary,
    ) -> str:
        """Compose user-facing text for a new-routine proposal.

        Returns a string ready for the principal cohort to surface.
        Closing question pinned via the template scaffold.
        """
        intent = self._intent_summary(draft)
        trigger = self._trigger_summary(draft)
        action = self._action_summary(draft)
        # Build the prompt the LLM fills. The template enforces the
        # structural shape; the LLM provides flowing variable parts.
        prompt = (
            f"Compose a brief, low-temperature proposal text using this "
            f"template structure:\n\n{_PROPOSAL_TEMPLATE}\n\n"
            f"Variables:\n"
            f"- intent_summary: {intent}\n"
            f"- trigger_summary: {trigger}\n"
            f"- action_summary: {action}\n\n"
            f"Return only the filled-in text."
        )
        text = await self._llm.complete(prompt)
        return self._verify_closing_question(text, fallback_template=_PROPOSAL_TEMPLATE.format(
            intent_summary=intent, trigger_summary=trigger, action_summary=action,
        ))

    # -- author_gap_message --------------------------------------------

    async def author_gap_message(
        self,
        *,
        capability_gap: "CapabilityGap",
        draft: "WorkflowDraft",
    ) -> str:
        """Compose user-facing language for a capability gap.

        Templated: mentions provider name + capability tag + offers
        both paths (set up vs. continue). Closing question pinned.
        """
        intent = self._intent_summary(draft)
        provider = self._provider_for_capability(capability_gap.required_tag)
        prompt = (
            f"Compose a brief gap-detection message using this "
            f"template structure:\n\n{_GAP_TEMPLATE}\n\n"
            f"Variables:\n"
            f"- provider_name: {provider}\n"
            f"- intent_summary: {intent}\n"
            f"- capability_tag: {capability_gap.required_tag}\n\n"
            f"Return only the filled-in text."
        )
        text = await self._llm.complete(prompt)
        return self._verify_closing_question(text, fallback_template=_GAP_TEMPLATE.format(
            provider_name=provider, intent_summary=intent,
            capability_tag=capability_gap.required_tag,
        ))

    # -- author_disambiguation -----------------------------------------

    async def author_disambiguation(
        self,
        *,
        candidate_intents: "list[CandidateIntent]",
        ambiguity_kind: AmbiguityKind | None = None,
    ) -> str:
        """Compose user-facing disambiguation question.

        Routes by ``ambiguity_kind``:

        * ``'multiple_intents'``: "a few things" framing for new-intent
          ambiguity.
        * ``'modification_target'``: "a few existing routines" framing
          for modification-target ambiguity.

        When ``ambiguity_kind`` is ``None``, infers from the presence
        of ``target_workflow_id`` on the candidates: if any candidate
        has one, modification-target framing wins; otherwise multiple-
        intents framing.
        """
        if ambiguity_kind is None:
            ambiguity_kind = self._infer_ambiguity_kind(candidate_intents)
        candidate_summaries = "; ".join(
            f"({c.summary})" for c in candidate_intents
        )
        if ambiguity_kind == "modification_target":
            template = _DISAMBIGUATION_MODIFICATION_TEMPLATE
        else:
            template = _DISAMBIGUATION_MULTIPLE_TEMPLATE
        prompt = (
            f"Compose a brief disambiguation question using this "
            f"template structure:\n\n{template}\n\n"
            f"Variables:\n"
            f"- candidate_summaries: {candidate_summaries}\n\n"
            f"Return only the filled-in text."
        )
        text = await self._llm.complete(prompt)
        return self._verify_closing_question(text, fallback_template=template.format(
            candidate_summaries=candidate_summaries,
        ))

    # -- author_modification_proposal ----------------------------------

    async def author_modification_proposal(
        self,
        *,
        draft: "WorkflowDraft",
        prev_workflow: "Workflow",
    ) -> str:
        """Compose user-facing language for a routine modification.

        Patch-first framing: narrate the diff in user terms.
        """
        routine_name = (
            getattr(prev_workflow, "name", "")
            or getattr(prev_workflow, "display_name", "")
            or "routine"
        )
        diff = self._diff_summary(draft, prev_workflow)
        prompt = (
            f"Compose a brief routine-modification proposal using "
            f"this template structure:\n\n{_MODIFICATION_TEMPLATE}\n\n"
            f"Variables:\n"
            f"- routine_name: {routine_name}\n"
            f"- diff_summary: {diff}\n\n"
            f"Return only the filled-in text."
        )
        text = await self._llm.complete(prompt)
        return self._verify_closing_question(text, fallback_template=_MODIFICATION_TEMPLATE.format(
            routine_name=routine_name, diff_summary=diff,
        ))

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _intent_summary(draft: "WorkflowDraft") -> str:
        return (draft.intent_summary or "this routine").strip()

    @staticmethod
    def _trigger_summary(draft: "WorkflowDraft") -> str:
        spec = draft.partial_spec_json or {}
        triggers = spec.get("triggers") or []
        if not triggers or not isinstance(triggers, list):
            return "(no trigger declared)"
        first = triggers[0] if isinstance(triggers[0], dict) else {}
        return str(first.get("event_type") or "(unspecified)")

    @staticmethod
    def _action_summary(draft: "WorkflowDraft") -> str:
        spec = draft.partial_spec_json or {}
        actions = spec.get("action_sequence") or []
        if not actions:
            return "(no action declared)"
        kinds = [
            a.get("action_type") for a in actions
            if isinstance(a, dict) and a.get("action_type")
        ]
        if not kinds:
            return "(unspecified)"
        return ", ".join(kinds)

    @staticmethod
    def _provider_for_capability(capability_tag: str) -> str:
        """Map a capability tag to a human-recognizable provider name.
        v1 inline mapping; future versions can consult ProviderRegistry
        for the active provider."""
        mapping = {
            "sms.send": "Twilio",
            "sms.read": "Twilio",
            "email.send": "Gmail",
            "email.read": "Gmail",
            "calendar.read": "Calendar",
            "calendar.write": "Calendar",
        }
        return mapping.get(capability_tag, capability_tag.split(".", 1)[0].title())

    @staticmethod
    def _diff_summary(draft: "WorkflowDraft", prev_workflow: "Workflow") -> str:
        """v1 deterministic diff narration. Returns a short
        human-readable description of what's changing."""
        spec = draft.partial_spec_json or {}
        new_intent = (draft.intent_summary or "").strip()
        prev_name = getattr(prev_workflow, "name", "") or ""
        diff_parts = []
        if new_intent and new_intent != prev_name:
            diff_parts.append(f"intent now '{new_intent}'")
        new_triggers = spec.get("triggers") or []
        if new_triggers:
            evs = [
                t.get("event_type") for t in new_triggers
                if isinstance(t, dict) and t.get("event_type")
            ]
            if evs:
                diff_parts.append(f"triggers: {', '.join(evs)}")
        return "; ".join(diff_parts) if diff_parts else "minor adjustments"

    @staticmethod
    def _infer_ambiguity_kind(
        candidate_intents: "list[CandidateIntent]",
    ) -> AmbiguityKind:
        for c in candidate_intents:
            if getattr(c, "target_workflow_id", None):
                return "modification_target"
        return "multiple_intents"

    @staticmethod
    def _verify_closing_question(
        text: str, *, fallback_template: str,
    ) -> str:
        """Defensive guard: if the LLM dropped the closing question,
        fall back to the templated form. Keeps AC #5 invariant
        regardless of LLM behavior."""
        if not text or not text.strip():
            return fallback_template
        for marker in _CLOSING_QUESTION_MARKERS:
            if marker in text:
                return text
        # No question mark present — append the templated tail. v1
        # conservative behavior; future versions can retry.
        return f"{text.rstrip()} {fallback_template.split('. ')[-1]}"


__all__ = [
    "AmbiguityKind",
    "CRBProposalAuthor",
    "CapabilityStateSummary",
    "LLMClient",
    "LLMResponseMalformed",
    "LLMTemperatureTooHigh",
    "MAX_TEMPERATURE",
    "ProposalAuthoringError",
    "TemplateRegistryEmpty",
]
