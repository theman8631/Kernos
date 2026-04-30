"""Approval-event validation for STS register_workflow.

Implements steps 2-5b of the production registration flow:

* Step 2: resolve the approval event in the event stream.
* Step 3: validate envelope source authority (substrate-set
  ``source_module == "crb"``). Reads from the substrate envelope,
  NEVER from caller-supplied payload.
* Step 4: validate proposal anchor (correlation_id resolves to a
  ``routine.proposed`` event with matching descriptor_hash and
  instance_id, also envelope-source-checked).
* Step 5: validate approval-call instance match.
* Step 5b: modification target binding — for ``routine.modification.approved``,
  ``payload.prev_workflow_id`` must equal ``descriptor.prev_version_id``
  AND the target workflow must exist in the same instance.

Steps 6-9 (revalidation, hash match, atomic persist + consumption) live
in :mod:`kernos.kernel.substrate_tools.registration.register`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.kernel import event_stream
from kernos.kernel.substrate_tools.errors import (
    ApprovalAuthorityIncomplete,
    ApprovalAuthoritySpoofed,
    ApprovalEventNotFound,
    ApprovalEventTypeInvalid,
    ApprovalInstanceMismatch,
    ApprovalModificationTargetMismatch,
    ApprovalModificationTargetMissing,
    ApprovalProposalMismatch,
    ApprovalProvenanceUnverifiable,
)

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.event_stream import Event
    from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


CRB_SOURCE_MODULE = "crb"
APPROVAL_EVENT_TYPES = frozenset({
    "routine.approved",
    "routine.modification.approved",
})
PROPOSAL_EVENT_TYPE = "routine.proposed"


_REQUIRED_APPROVAL_FIELDS = frozenset({
    "approved_by",
    "member_id",
    "source_turn_id",
    "correlation_id",
    "descriptor_hash",
    "instance_id",
})


async def resolve_and_validate_approval(
    *,
    instance_id: str,
    approval_event_id: str,
    descriptor: dict,
    workflow_registry: "WorkflowRegistry",
) -> "Event":
    """Run steps 2-5b. Returns the validated approval event for the
    caller (register.py) to use in steps 6-9. Raises a typed
    :class:`SubstrateToolsError` subclass on any check failure.
    """
    # Step 2: resolve the approval event.
    approval_event = await event_stream.event_by_id(instance_id, approval_event_id)
    if approval_event is None:
        raise ApprovalEventNotFound(
            f"approval_event_id={approval_event_id!r} did not resolve "
            f"in instance={instance_id!r}"
        )
    if approval_event.event_type not in APPROVAL_EVENT_TYPES:
        raise ApprovalEventTypeInvalid(
            f"event {approval_event_id!r} has type "
            f"{approval_event.event_type!r}; expected one of "
            f"{sorted(APPROVAL_EVENT_TYPES)}"
        )

    # Step 3: envelope source authority. Read from substrate envelope,
    # NEVER payload — this is the spec's load-bearing trust boundary.
    if approval_event.envelope.source_module != CRB_SOURCE_MODULE:
        raise ApprovalAuthoritySpoofed(
            f"approval event {approval_event_id!r} envelope.source_module="
            f"{approval_event.envelope.source_module!r}; "
            f"expected {CRB_SOURCE_MODULE!r}"
        )

    # Step 3b: required provenance fields present and non-empty.
    payload = approval_event.payload or {}
    missing = [f for f in _REQUIRED_APPROVAL_FIELDS if not payload.get(f)]
    if missing:
        raise ApprovalAuthorityIncomplete(
            f"approval event {approval_event_id!r} missing required "
            f"provenance fields: {sorted(missing)}"
        )

    # Step 4: proposal anchor resolution.
    correlation_id = payload["correlation_id"]
    correlated = await event_stream.events_by_correlation(
        instance_id, correlation_id,
    )
    proposed = next(
        (e for e in correlated if e.event_type == PROPOSAL_EVENT_TYPE), None,
    )
    if proposed is None:
        raise ApprovalProvenanceUnverifiable(
            f"correlation_id={correlation_id!r} from approval "
            f"{approval_event_id!r} does not resolve to a "
            f"{PROPOSAL_EVENT_TYPE!r} event in instance {instance_id!r}"
        )
    if proposed.envelope.source_module != CRB_SOURCE_MODULE:
        raise ApprovalProvenanceUnverifiable(
            f"proposed event {proposed.event_id!r} envelope.source_module="
            f"{proposed.envelope.source_module!r}; expected {CRB_SOURCE_MODULE!r}"
        )

    # Step 4b: descriptor_hash and instance_id must agree across the chain.
    proposed_payload = proposed.payload or {}
    if proposed_payload.get("descriptor_hash") != payload.get("descriptor_hash"):
        raise ApprovalProposalMismatch(
            f"proposed.descriptor_hash="
            f"{proposed_payload.get('descriptor_hash')!r} != "
            f"approved.descriptor_hash={payload.get('descriptor_hash')!r}"
        )
    if proposed_payload.get("instance_id") != payload.get("instance_id"):
        raise ApprovalInstanceMismatch(
            f"proposed.instance_id={proposed_payload.get('instance_id')!r} "
            f"!= approved.instance_id={payload.get('instance_id')!r}"
        )

    # Step 5: approval's instance_id must match the calling instance.
    if payload["instance_id"] != instance_id:
        raise ApprovalInstanceMismatch(
            f"approval.instance_id={payload['instance_id']!r} != "
            f"caller instance_id={instance_id!r}"
        )

    # Step 5b: modification target binding (Kit edit v1 → v2).
    if approval_event.event_type == "routine.modification.approved":
        prev_workflow_id = payload.get("prev_workflow_id") or ""
        if not prev_workflow_id:
            raise ApprovalAuthorityIncomplete(
                f"modification approval {approval_event_id!r} missing "
                f"required field 'prev_workflow_id'"
            )
        descriptor_prev = descriptor.get("prev_version_id") or ""
        if not descriptor_prev or descriptor_prev != prev_workflow_id:
            raise ApprovalModificationTargetMismatch(
                f"approval.prev_workflow_id={prev_workflow_id!r} != "
                f"descriptor.prev_version_id={descriptor_prev!r}"
            )
        target = await workflow_registry.get_workflow(prev_workflow_id)
        if target is None or target.instance_id != instance_id:
            raise ApprovalModificationTargetMissing(
                f"modification approval references prev_workflow_id="
                f"{prev_workflow_id!r} which does not exist in instance "
                f"{instance_id!r}"
            )

    return approval_event


__all__ = [
    "APPROVAL_EVENT_TYPES",
    "CRB_SOURCE_MODULE",
    "PROPOSAL_EVENT_TYPE",
    "resolve_and_validate_approval",
]
