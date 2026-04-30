"""CRB event emission (CRB C5).

Concrete adapter that implements :class:`CRBEventPort` against the
event_stream substrate. Engine bring-up registers ``"crb"`` with the
EmitterRegistry once and constructs :class:`CRBEventEmitter` around
the returned :class:`EventEmitter`. CRBApprovalFlow + CRBProposalAuthor
take the adapter via the typed port; the raw :class:`EventEmitter` is
NEVER injected directly.

Substrate enforcement:

* EmitterRegistry uniqueness ensures at most one emitter claims
  ``source_module="crb"``.
* ``EventEmitter.emit`` stamps ``envelope.source_module="crb"`` from
  the registered identity, NOT from caller payload — closes the
  spoof vector for STS approval source authority.
* The adapter exposes ONLY the five typed CRB emission methods. Raw
  ``emit`` is structurally absent from the port surface.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.kernel.crb.approval.ports import CRBEventPort

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.event_stream import EventEmitter


CRB_SOURCE_MODULE = "crb"


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


EVENT_ROUTINE_PROPOSED = "routine.proposed"
EVENT_ROUTINE_APPROVED = "routine.approved"
EVENT_ROUTINE_MODIFICATION_APPROVED = "routine.modification.approved"
EVENT_ROUTINE_DECLINED = "routine.declined"
EVENT_CRB_FEEDBACK_MODIFY_REQUEST = "crb.feedback.modify_request"


CRB_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_ROUTINE_PROPOSED,
    EVENT_ROUTINE_APPROVED,
    EVENT_ROUTINE_MODIFICATION_APPROVED,
    EVENT_ROUTINE_DECLINED,
    EVENT_CRB_FEEDBACK_MODIFY_REQUEST,
})


# ---------------------------------------------------------------------------
# CRBEventEmitter
# ---------------------------------------------------------------------------


class CRBEventEmitter:
    """Concrete :class:`CRBEventPort` adapter.

    Constructor takes the registered :class:`EventEmitter` (with
    ``source_module="crb"``). The adapter verifies the source identity
    at construction so misconfiguration surfaces at engine bring-up,
    not at first emission.
    """

    def __init__(self, *, emitter: "EventEmitter") -> None:
        if emitter.source_module != CRB_SOURCE_MODULE:
            raise ValueError(
                f"CRBEventEmitter requires an emitter registered with "
                f"source_module={CRB_SOURCE_MODULE!r}, got "
                f"{emitter.source_module!r}"
            )
        self._emitter = emitter

    @property
    def source_module(self) -> str:
        return self._emitter.source_module

    # --------------------------------------------------------------
    # Emit helpers — one per CRB event type. Each returns the
    # substrate-set event_id so the caller (CRBApprovalFlow) can use
    # it as the approval_event_id for STS register_workflow.
    # --------------------------------------------------------------

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
        payload = {
            "correlation_id": correlation_id,
            "proposal_id": proposal_id,
            "instance_id": instance_id,
            "draft_id": draft_id,
            "descriptor_hash": descriptor_hash,
            "member_id": member_id,
            "source_thread_id": source_thread_id,
            "proposed_by": "crb",
            "prev_workflow_id": prev_workflow_id,
        }
        return await self._emitter.emit(
            instance_id, EVENT_ROUTINE_PROPOSED, payload,
            correlation_id=correlation_id,
        )

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
        payload = {
            "correlation_id": correlation_id,
            "proposal_id": proposal_id,
            "instance_id": instance_id,
            "descriptor_hash": descriptor_hash,
            "member_id": member_id,
            "source_turn_id": source_turn_id,
            "approved_by": "crb",
        }
        return await self._emitter.emit(
            instance_id, EVENT_ROUTINE_APPROVED, payload,
            correlation_id=correlation_id,
        )

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
        payload = {
            "correlation_id": correlation_id,
            "proposal_id": proposal_id,
            "instance_id": instance_id,
            "descriptor_hash": descriptor_hash,
            "prev_workflow_id": prev_workflow_id,
            "change_summary": change_summary,
            "member_id": member_id,
            "source_turn_id": source_turn_id,
            "approved_by": "crb",
        }
        return await self._emitter.emit(
            instance_id, EVENT_ROUTINE_MODIFICATION_APPROVED, payload,
            correlation_id=correlation_id,
        )

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
        payload = {
            "correlation_id": correlation_id,
            "proposal_id": proposal_id,
            "instance_id": instance_id,
            "draft_id": draft_id,
            "decline_reason": decline_reason,
            "member_id": member_id,
        }
        return await self._emitter.emit(
            instance_id, EVENT_ROUTINE_DECLINED, payload,
            correlation_id=correlation_id,
        )

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
        payload = {
            "instance_id": instance_id,
            "draft_id": draft_id,
            "original_proposal_id": original_proposal_id,
            "feedback_summary": feedback_summary,
            "source_turn_id": source_turn_id,
            "member_id": member_id,
        }
        return await self._emitter.emit(
            instance_id, EVENT_CRB_FEEDBACK_MODIFY_REQUEST, payload,
        )


__all__ = [
    "CRB_EVENT_TYPES",
    "CRB_SOURCE_MODULE",
    "CRBEventEmitter",
    "EVENT_CRB_FEEDBACK_MODIFY_REQUEST",
    "EVENT_ROUTINE_APPROVED",
    "EVENT_ROUTINE_DECLINED",
    "EVENT_ROUTINE_MODIFICATION_APPROVED",
    "EVENT_ROUTINE_PROPOSED",
]
