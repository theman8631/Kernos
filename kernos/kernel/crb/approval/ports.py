"""CRBApprovalFlow port protocols (CRB C4).

Restricted ports the flow takes as dependencies. The Protocol shape
makes test injection trivial; production wiring at engine bring-up
constructs concrete adapters around STS / WDP / event emitter.

Why ports vs raw deps: the spec calls out crash-safe handoff between
approval emission and STS registration. Each side effect routes
through a typed port so tests can drive failure scenarios (transient
STS errors, ApprovalAlreadyConsumed races) without pulling in the
full STS stack.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import WorkflowDraft
    from kernos.kernel.workflows.workflow_registry import Workflow


# ---------------------------------------------------------------------------
# DraftReadPort — read-only view of WDP for state checks
# ---------------------------------------------------------------------------


@runtime_checkable
class DraftReadPort(Protocol):
    """Restricted facade over WDP for read-only queries that
    CRBApprovalFlow needs (draft existence + state + descriptor hash
    derivation).

    Structurally absent: any write capability. CRB never mutates WDP
    directly; Drafter's port handles writes (and the Drafter-mediated
    abandon path is principal-driven anyway).
    """

    async def get_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
    ) -> "WorkflowDraft | None":
        ...


# ---------------------------------------------------------------------------
# STSRegistrationPort — register + find_workflow_by_approval_event_id
# ---------------------------------------------------------------------------


@runtime_checkable
class STSRegistrationPort(Protocol):
    """Restricted facade over STS for the two surfaces CRB needs."""

    async def register_workflow(
        self,
        *,
        instance_id: str,
        descriptor: dict,
        approval_event_id: str,
    ) -> "Workflow":
        """Production registration. Raises ApprovalAlreadyConsumed on
        race / replay; raises STSTransientError on transient failure
        (e.g. backend connectivity)."""

    async def find_workflow_by_approval_event_id(
        self,
        *,
        instance_id: str,
        approval_event_id: str,
    ) -> "Workflow | None":
        """Lookup for crash-recovery sweep. None if not yet registered."""


# ---------------------------------------------------------------------------
# CRBEventPort — restricted emit surface
# ---------------------------------------------------------------------------


@runtime_checkable
class CRBEventPort(Protocol):
    """Restricted facade over the registered ``"crb"`` EventEmitter.

    Structurally absent: raw emit. Only the five typed emission
    methods that match the CRB v1 event taxonomy. C5 ships the
    concrete adapter; C4 wires CRBApprovalFlow against the protocol.

    Each method returns the substrate-set ``event_id`` so the flow
    can correlate (approval_event_id is the event_id of the emitted
    routine.approved / routine.modification.approved).
    """

    async def emit_routine_proposed(
        self,
        *,
        correlation_id: str,
        proposal_id: str,
        instance_id: str,
        draft_id: str,
        descriptor_hash: str,
        member_id: str,
        source_thread_id: str,
        prev_workflow_id: str | None = None,
    ) -> str:
        ...

    async def emit_routine_approved(
        self,
        *,
        correlation_id: str,
        proposal_id: str,
        instance_id: str,
        descriptor_hash: str,
        member_id: str,
        source_turn_id: str,
    ) -> str:
        ...

    async def emit_routine_modification_approved(
        self,
        *,
        correlation_id: str,
        proposal_id: str,
        instance_id: str,
        descriptor_hash: str,
        prev_workflow_id: str,
        change_summary: str,
        member_id: str,
        source_turn_id: str,
    ) -> str:
        ...

    async def emit_routine_declined(
        self,
        *,
        correlation_id: str,
        proposal_id: str,
        instance_id: str,
        draft_id: str,
        decline_reason: str,
        member_id: str,
    ) -> str:
        ...

    async def emit_crb_feedback_modify_request(
        self,
        *,
        instance_id: str,
        draft_id: str,
        original_proposal_id: str,
        feedback_summary: str,
        source_turn_id: str,
        member_id: str,
    ) -> str:
        ...


# ---------------------------------------------------------------------------
# Errors STSRegistrationPort callers can encounter
# ---------------------------------------------------------------------------


class STSTransientError(Exception):
    """Raised by STSRegistrationPort.register_workflow when the
    registration could not complete due to a transient condition
    (e.g. backend unreachable). The caller MUST leave the
    install_proposals state in approved_pending_registration; the
    recovery sweep retries on next engine startup or via Case 1b
    duplicate-yes-during-pending."""


__all__ = [
    "CRBEventPort",
    "DraftReadPort",
    "STSRegistrationPort",
    "STSTransientError",
]
