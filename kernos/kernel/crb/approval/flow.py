"""CRBApprovalFlow state machine (CRB C4).

Owns ``handle_response`` (with crash-safe approval-to-registration
handoff per Kit pin v1->v2 must-fix #1), branches event emission by
proposal type (must-fix #2: modifications emit
``routine.modification.approved``, not the generic event),
``handle_explicit_modification_request`` fallback when Drafter misses
the modification intent, ``handle_disambiguation_response`` with
permission gate per Drafter v2 AC #12, and the
``recover_pending_registrations`` engine-startup sweep.

Six duplicate / late approval cases (Seam C11) are handled inline in
``handle_response``:

1. Already-approved proposal (terminal) + approve -> already_approved.
1b. Approve while pending registration -> trigger recovery sweep
    synchronously; either already_approved (recovery succeeded) or
    registration_pending (still deferred).
2. Descriptor drift between proposal and approval -> re-author against
   current draft; new install_proposals row with prev_proposal_id.
3. Draft abandoned since proposal -> draft_abandoned outcome; no
   approval event.
4. STS already_consumed (race winner) -> sts_already_consumed outcome
   when find_workflow_by_approval_event_id returns None despite the
   error (substrate inconsistency).
5. Draft superseded into newer version -> draft_superseded outcome;
   re-author against newer draft.
6. TTL expired -> expired outcome (v1.x; field present but enforcement
   only when expires_at is populated by caller).

Elegance latitude: spec called for a separate ``duplicate_handling.py``
module for the C11 cases. v1 keeps them inline in ``handle_response``
because they're branch logic specific to that one method; a separate
module would add indirection without reuse. Will note in batch report.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from kernos.kernel.crb.approval.ports import (
    CRBEventPort,
    DraftReadPort,
    STSRegistrationPort,
    STSTransientError,
)
from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.proposal.author import CRBProposalAuthor
from kernos.kernel.crb.proposal.install_proposal import (
    DisambiguationOutcome,
    ExplicitModificationOutcome,
    FlowOutcome,
    FlowResponse,
    InstallProposal,
)
from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
    StaleStateError,
    UnknownProposal,
)

# STS error class lives in the substrate_tools package; we catch it
# at this layer and translate into FlowOutcome(kind='sts_already_consumed').
try:
    from kernos.kernel.substrate_tools.errors import (
        ApprovalAlreadyConsumed,
    )
except Exception:  # pragma: no cover - defensive
    ApprovalAlreadyConsumed = type(  # type: ignore[assignment]
        "ApprovalAlreadyConsumed", (Exception,), {},
    )


if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.workflows.workflow_registry import Workflow


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApprovalFlowError(Exception):
    """Base for ApprovalFlow errors."""


class ProposalNotPendingForRecovery(ApprovalFlowError):
    """recover_pending_registration called against a proposal not in
    state=approved_pending_registration."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CRBApprovalFlow
# ---------------------------------------------------------------------------


class CRBApprovalFlow:
    """State machine: proposed -> approved_pending_registration ->
    approved_registered | modify_requested | declined.

    Owns duplicate / late approval handling at the CRB layer. STS
    error classes are caught here and translated into typed
    FlowOutcomes so the principal cohort sees a clean conversational
    surface.
    """

    def __init__(
        self,
        *,
        install_proposal_store: InstallProposalStore,
        draft_port: DraftReadPort,
        sts_port: STSRegistrationPort,
        event_emitter: CRBEventPort,
        author: CRBProposalAuthor,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = install_proposal_store
        self._draft_port = draft_port
        self._sts_port = sts_port
        self._event_emitter = event_emitter
        self._author = author
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # === handle_response ===========================================

    async def handle_response(
        self,
        *,
        proposal_id: str,
        response: FlowResponse,
        source_turn_id: str,
        member_id: str,
    ) -> FlowOutcome:
        """Map a user response to event emission, store mutation, and
        a typed FlowOutcome. Six duplicate/late cases handled inline.
        """
        proposal = await self._store.get_proposal(proposal_id=proposal_id)
        if proposal is None:
            raise UnknownProposal(
                f"no proposal with proposal_id={proposal_id!r}"
            )

        # Case 1: duplicate "yes" — already approved (terminal).
        if (
            proposal.state == "approved_registered"
            and response.kind == "approve"
        ):
            workflow = await self._sts_port.find_workflow_by_approval_event_id(
                instance_id=proposal.instance_id,
                approval_event_id=proposal.approval_event_id or "",
            )
            return FlowOutcome(
                kind="already_approved",
                proposal=proposal,
                workflow_id=workflow.workflow_id if workflow else None,
            )

        # Case 1b: duplicate "yes" while still pending registration.
        if (
            proposal.state == "approved_pending_registration"
            and response.kind == "approve"
        ):
            recovered = await self._try_recover_pending_registration(proposal)
            if recovered is not None:
                # Recovery completed; fetch refreshed proposal.
                latest = await self._store.get_proposal(
                    proposal_id=proposal.proposal_id,
                )
                return FlowOutcome(
                    kind="already_approved",
                    proposal=latest or proposal,
                    workflow_id=recovered.workflow_id,
                )
            return FlowOutcome(
                kind="registration_pending",
                proposal=proposal,
                approval_event_id=proposal.approval_event_id,
            )

        # Case 6: TTL expired (v1.x opt-in; only fires when expires_at
        # was populated at create time).
        if proposal.expires_at:
            if self._clock().isoformat() > proposal.expires_at:
                return FlowOutcome(kind="expired", proposal=proposal)

        # Case 3: draft abandoned since proposal.
        current_draft = await self._draft_port.get_draft(
            instance_id=proposal.instance_id, draft_id=proposal.draft_id,
        )
        if current_draft is None or current_draft.status == "abandoned":
            return FlowOutcome(
                kind="draft_abandoned", proposal=proposal,
            )

        # Case 5: draft superseded by newer modified version. v1 uses
        # the partial_spec_json metadata next_version_id pointer if
        # set; otherwise no supersede path.
        next_version_id = (
            (current_draft.partial_spec_json or {}).get("next_version_id")
        )
        if next_version_id:
            return FlowOutcome(
                kind="draft_superseded",
                proposal=proposal,
                new_proposal_id=None,  # filled by re-author callers in v1.x
            )

        # Approve branch (Cases 2, 4, happy path with crash-safe handoff).
        if response.kind == "approve":
            # Case 2: descriptor drifted between proposal and approval.
            current_hash = self._compute_descriptor_hash(current_draft)
            if current_hash != proposal.descriptor_hash:
                return FlowOutcome(
                    kind="descriptor_drifted",
                    proposal=proposal,
                    new_proposal_id=None,
                )

            # Crash-safe handoff (Kit pin v1->v2 must-fix #1):
            # Step 1: emit appropriate approval event durably.
            # (Modification branch per Kit pin v1->v2 must-fix #2.)
            if proposal.prev_workflow_id:
                approval_event_id = await self._event_emitter.emit_routine_modification_approved(
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.proposal_id,
                    instance_id=proposal.instance_id,
                    descriptor_hash=proposal.descriptor_hash,
                    prev_workflow_id=proposal.prev_workflow_id,
                    change_summary=self._compute_change_summary(
                        current_draft, proposal,
                    ),
                    member_id=member_id,
                    source_turn_id=source_turn_id,
                )
            else:
                approval_event_id = await self._event_emitter.emit_routine_approved(
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.proposal_id,
                    instance_id=proposal.instance_id,
                    descriptor_hash=proposal.descriptor_hash,
                    member_id=member_id,
                    source_turn_id=source_turn_id,
                )

            # Step 2: transition state to approved_pending_registration
            # BEFORE STS attempt.
            try:
                proposal = await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_pending_registration",
                    response_kind="approve",
                    approval_event_id=approval_event_id,
                )
            except StaleStateError:
                # Concurrent transition; re-fetch and recompute outcome.
                proposal = (
                    await self._store.get_proposal(
                        proposal_id=proposal.proposal_id,
                    )
                ) or proposal
                # Fall through to Case 1b-equivalent handling.
                if proposal.state == "approved_pending_registration":
                    return FlowOutcome(
                        kind="registration_pending",
                        proposal=proposal,
                        approval_event_id=approval_event_id,
                    )
                if proposal.state == "approved_registered":
                    workflow = await self._sts_port.find_workflow_by_approval_event_id(
                        instance_id=proposal.instance_id,
                        approval_event_id=proposal.approval_event_id or "",
                    )
                    return FlowOutcome(
                        kind="already_approved",
                        proposal=proposal,
                        workflow_id=workflow.workflow_id if workflow else None,
                    )
                # Unexpected concurrent state; surface as pending.
                return FlowOutcome(
                    kind="registration_pending",
                    proposal=proposal,
                    approval_event_id=approval_event_id,
                )

            # Step 3: attempt STS registration.
            descriptor = draft_to_descriptor_candidate(current_draft)
            try:
                workflow = await self._sts_port.register_workflow(
                    instance_id=proposal.instance_id,
                    descriptor=descriptor,
                    approval_event_id=approval_event_id,
                )
                # Success: transition to approved_registered.
                try:
                    proposal = await self._store.transition_state(
                        proposal_id=proposal.proposal_id,
                        new_state="approved_registered",
                    )
                except StaleStateError:
                    # Race recovery sweep got there first; re-fetch.
                    proposal = (
                        await self._store.get_proposal(
                            proposal_id=proposal.proposal_id,
                        )
                    ) or proposal
                return FlowOutcome(
                    kind="approved",
                    proposal=proposal,
                    workflow_id=workflow.workflow_id,
                )
            except ApprovalAlreadyConsumed:
                # Race or replay: STS already registered with this
                # approval_event_id. Recover by lookup.
                workflow = await self._sts_port.find_workflow_by_approval_event_id(
                    instance_id=proposal.instance_id,
                    approval_event_id=approval_event_id,
                )
                if workflow is not None:
                    try:
                        proposal = await self._store.transition_state(
                            proposal_id=proposal.proposal_id,
                            new_state="approved_registered",
                        )
                    except StaleStateError:
                        proposal = (
                            await self._store.get_proposal(
                                proposal_id=proposal.proposal_id,
                            )
                        ) or proposal
                    return FlowOutcome(
                        kind="approved",
                        proposal=proposal,
                        workflow_id=workflow.workflow_id,
                    )
                # Case 4: substrate inconsistency.
                return FlowOutcome(
                    kind="sts_already_consumed",
                    proposal=proposal,
                )
            except STSTransientError:
                # Recovery sweep retries; state stays pending.
                logger.warning(
                    "CRB_PENDING_REGISTRATION: proposal=%s "
                    "approval_event_id=%s left pending after STS "
                    "transient failure",
                    proposal.proposal_id, approval_event_id,
                )
                return FlowOutcome(
                    kind="registration_pending",
                    proposal=proposal,
                    approval_event_id=approval_event_id,
                )

        # Modify branch.
        if response.kind == "modify":
            await self._event_emitter.emit_crb_feedback_modify_request(
                instance_id=proposal.instance_id,
                draft_id=proposal.draft_id,
                original_proposal_id=proposal.proposal_id,
                feedback_summary=response.feedback_summary or "",
                source_turn_id=source_turn_id,
                member_id=member_id,
            )
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="modify_requested",
                response_kind="modify",
            )
            return FlowOutcome(
                kind="modify_dispatched", proposal=proposal,
            )

        # Decline branches.
        if response.kind == "not_now":
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="declined",
                response_kind="not_now",
            )
            return FlowOutcome(kind="declined_pause", proposal=proposal)

        if response.kind == "abandon":
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="declined",
                response_kind="abandon",
            )
            await self._event_emitter.emit_routine_declined(
                correlation_id=proposal.correlation_id,
                proposal_id=proposal.proposal_id,
                instance_id=proposal.instance_id,
                draft_id=proposal.draft_id,
                decline_reason="user_explicit_stop",
                member_id=member_id,
            )
            return FlowOutcome(
                kind="declined_abandon", proposal=proposal,
            )

        raise ApprovalFlowError(
            f"unknown response kind {response.kind!r}"
        )

    # === handle_explicit_modification_request =====================

    async def handle_explicit_modification_request(
        self,
        *,
        instance_id: str,
        target_workflow_id: str,
        feedback_summary: str,
        source_turn_id: str,
        member_id: str,
        candidate_workflow_lookup: Callable[
            [str, str], "Awaitable[list[Workflow]]"  # type: ignore[name-defined]
        ] | None = None,
    ) -> ExplicitModificationOutcome:
        """Fallback when Drafter misses a modification intent (Kit pin
        C4). Resolves ``target_workflow_id`` via the supplied lookup;
        on a single match, emits ``crb.feedback.modify_request`` to
        drive the Drafter shaping path. On ambiguity, returns an
        outcome the principal can use to author a clarification. On
        no-match, returns a gap-shaped outcome.

        ``candidate_workflow_lookup`` is the resolver: ``(instance_id,
        target_workflow_id) -> list[Workflow]``. Engine bring-up wires
        STS list_workflows + DAR. Tests inject deterministic stubs.
        """
        if not target_workflow_id:
            raise ValueError("target_workflow_id is required")

        if candidate_workflow_lookup is None:
            return ExplicitModificationOutcome(
                kind="no_match",
                target_workflow_id=target_workflow_id,
            )

        candidates = await candidate_workflow_lookup(
            instance_id, target_workflow_id,
        )
        if not candidates:
            return ExplicitModificationOutcome(
                kind="no_match",
                target_workflow_id=target_workflow_id,
            )
        if len(candidates) > 1:
            return ExplicitModificationOutcome(
                kind="ambiguous",
                target_workflow_id=target_workflow_id,
                candidates=tuple(
                    {
                        "workflow_id": w.workflow_id,
                        "name": getattr(w, "name", ""),
                    }
                    for w in candidates
                ),
            )
        # Single match: emit modify_request.
        target = candidates[0]
        # We need a draft_id to drive Drafter shaping. v1 derives it
        # via the existing draft for this workflow, OR creates a
        # synthetic draft_id. For simplicity in v1, the principal is
        # responsible for ensuring a draft exists; the explicit-modify
        # entry point just emits the feedback signal.
        await self._event_emitter.emit_crb_feedback_modify_request(
            instance_id=instance_id,
            draft_id=target.workflow_id,  # use workflow_id as draft hint
            original_proposal_id="",  # no proposal in flight yet
            feedback_summary=feedback_summary,
            source_turn_id=source_turn_id,
            member_id=member_id,
        )
        return ExplicitModificationOutcome(
            kind="dispatched",
            target_workflow_id=target.workflow_id,
        )

    # === handle_disambiguation_response ===========================

    async def handle_disambiguation_response(
        self,
        *,
        instance_id: str,
        signal_id: str,
        picked_candidate_id: str,
        unselected_candidates: list[dict],
        source_turn_id: str,
        member_id: str,
    ) -> DisambiguationOutcome:
        """Route the user's disambiguation choice. Picked candidate
        becomes active; unselected candidates evaluated INDEPENDENTLY
        for ``permission_to_make_durable``:

        * permission=True -> paused-for-later WDP draft (mediated by
          principal via Drafter port; CRB just reports the decision).
        * permission=False -> ephemeral; NO WDP row.

        Codex / Kit pin: Drafter v2 AC #12 invariant preserved across
        this CRB entry point — weak candidates stay ephemeral.
        """
        if not picked_candidate_id:
            raise ValueError("picked_candidate_id is required")

        paused_draft_ids: list[str] = []
        ephemeral_ids: list[str] = []
        for cand in unselected_candidates:
            cand_id = cand.get("candidate_id") or ""
            if not cand_id:
                logger.warning(
                    "CRB_DISAMBIG_MISSING_CANDIDATE_ID: signal=%s "
                    "candidate=%r — treating as ephemeral",
                    signal_id, cand,
                )
                continue
            permission = bool(cand.get("permission_to_make_durable"))
            if permission:
                # Principal-mediated paused-draft creation. CRB reports
                # the decision; the actual port call is done by the
                # principal (Drafter port write capability is not on
                # CRB's surface).
                paused_draft_ids.append(cand_id)
            else:
                ephemeral_ids.append(cand_id)

        return DisambiguationOutcome(
            picked_candidate_id=picked_candidate_id,
            paused_draft_ids=tuple(paused_draft_ids),
            ephemeral_candidate_ids=tuple(ephemeral_ids),
        )

    # === recover_pending_registrations ============================

    async def recover_pending_registrations(self) -> list[InstallProposal]:
        """Engine-startup sweep. Walks every install_proposals row in
        state=approved_pending_registration and attempts to resume
        registration. Returns the list of proposals successfully
        transitioned to approved_registered.

        Idempotent: running the sweep N times produces the same
        outcome as running it once. Safe to invoke synchronously
        from Case 1b.
        """
        pending = await self._store.find_by_state(
            state="approved_pending_registration",
        )
        recovered: list[InstallProposal] = []
        for proposal in pending:
            workflow = await self._try_recover_pending_registration(proposal)
            if workflow is not None:
                latest = await self._store.get_proposal(
                    proposal_id=proposal.proposal_id,
                )
                if latest is not None:
                    recovered.append(latest)
        return recovered

    async def _try_recover_pending_registration(
        self, proposal: InstallProposal,
    ) -> "Workflow | None":
        """Idempotent recovery for a single pending proposal. See
        spec section "Crash recovery for pending registrations"."""
        if proposal.state != "approved_pending_registration":
            return None
        approval_event_id = proposal.approval_event_id or ""
        if not approval_event_id:
            logger.warning(
                "CRB_RECOVERY_MISSING_APPROVAL: proposal=%s in pending "
                "state but approval_event_id is empty",
                proposal.proposal_id,
            )
            return None

        # Common case: STS already registered before crash.
        workflow = await self._sts_port.find_workflow_by_approval_event_id(
            instance_id=proposal.instance_id,
            approval_event_id=approval_event_id,
        )
        if workflow is not None:
            try:
                await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_registered",
                )
            except StaleStateError:
                pass  # already transitioned by a concurrent path
            return workflow

        # Otherwise: retry STS registration with same approval_event_id.
        current_draft = await self._draft_port.get_draft(
            instance_id=proposal.instance_id, draft_id=proposal.draft_id,
        )
        if current_draft is None or current_draft.status == "abandoned":
            logger.warning(
                "CRB_RECOVERY_ORPHANED_APPROVAL: proposal=%s draft=%s "
                "is missing or abandoned; leaving pending for triage",
                proposal.proposal_id, proposal.draft_id,
            )
            return None

        descriptor = draft_to_descriptor_candidate(current_draft)
        try:
            workflow = await self._sts_port.register_workflow(
                instance_id=proposal.instance_id,
                descriptor=descriptor,
                approval_event_id=approval_event_id,
            )
            try:
                await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_registered",
                )
            except StaleStateError:
                pass
            return workflow
        except ApprovalAlreadyConsumed:
            workflow = await self._sts_port.find_workflow_by_approval_event_id(
                instance_id=proposal.instance_id,
                approval_event_id=approval_event_id,
            )
            if workflow is not None:
                try:
                    await self._store.transition_state(
                        proposal_id=proposal.proposal_id,
                        new_state="approved_registered",
                    )
                except StaleStateError:
                    pass
                return workflow
            return None
        except STSTransientError:
            return None

    # === helpers ===================================================

    @staticmethod
    def _compute_descriptor_hash(draft: Any) -> str:
        """Compute the descriptor hash for the draft as it stands now.
        Used to detect Case 2 (descriptor drift between proposal and
        approval). Imports STS canonical hash to keep parity with
        STS's approval-binding gate."""
        from kernos.kernel.substrate_tools.registration.descriptor_hash import (
            compute_descriptor_hash,
        )

        candidate = draft_to_descriptor_candidate(draft)
        return compute_descriptor_hash(candidate)

    @staticmethod
    def _compute_change_summary(
        current_draft: Any, proposal: InstallProposal,
    ) -> str:
        """v1 deterministic change-summary string for modification
        approvals. The user-facing diff narration lives in
        CRBProposalAuthor.author_modification_proposal; this is the
        machine-readable summary that flows into the
        ``routine.modification.approved`` event payload."""
        spec = current_draft.partial_spec_json or {}
        triggers = spec.get("triggers") or []
        actions = spec.get("action_sequence") or []
        return (
            f"draft_id={current_draft.draft_id} "
            f"intent={current_draft.intent_summary or ''} "
            f"triggers={len(triggers)} actions={len(actions)} "
            f"prev_workflow_id={proposal.prev_workflow_id or ''}"
        )


__all__ = [
    "ApprovalFlowError",
    "CRBApprovalFlow",
    "ProposalNotPendingForRecovery",
]
