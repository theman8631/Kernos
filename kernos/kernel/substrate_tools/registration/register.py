"""STS register_workflow gate.

The production registration entry point. Implements the full 9-step
validation flow before persistence. ``dry_run=True`` runs the same
validation but skips approval-related steps and never persists.

Production flow:

1. Validate caller arguments.
2. Resolve approval event (approval.py).
3. Validate envelope source authority (approval.py).
4. Validate proposal anchor (approval.py).
5. Validate approval-call instance match (approval.py).
5b. Modification target binding (approval.py).
6. Re-run full descriptor validation NOW (P7) — NOT cached.
7. Compare descriptor_hash to approval event's hash.
8. Verify validation produced no error-severity issues.
9. Atomic persist + UNIQUE-constraint consumption.

Step 9 raises :class:`ApprovalAlreadyConsumed` (TERMINAL) when the
partial UNIQUE index ``idx_workflows_approval_unique`` rejects a
duplicate ``(instance_id, approval_event_id)``. Caller MUST NOT retry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Union

import aiosqlite

from kernos.kernel.substrate_tools.errors import (
    ApprovalAlreadyConsumed,
    ApprovalBindingMissing,
    ApprovalDescriptorMismatch,
    ApprovalInstanceMismatch,
    RegistrationValidationFailed,
)
from kernos.kernel.substrate_tools.registration.approval import (
    resolve_and_validate_approval,
)
from kernos.kernel.substrate_tools.registration.validation import (
    DryRunResult,
    run_full_validation,
)

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRegistry
    from kernos.kernel.workflows.workflow_registry import (
        Workflow,
        WorkflowRegistry,
    )


async def register_workflow(
    *,
    instance_id: str,
    descriptor: dict,
    workflow_registry: "WorkflowRegistry",
    agent_registry: "AgentRegistry | None" = None,
    dry_run: bool = False,
    approval_event_id: str | None = None,
) -> "Union[Workflow, DryRunResult]":
    """Production-path registration gate. See module docstring for the
    9-step flow.

    Args:
        instance_id: caller's instance scope.
        descriptor: workflow descriptor dict.
        workflow_registry: WLP instance for the underlying registration
            and modification-target lookup.
        agent_registry: DAR instance for route_to_agent reference
            validation. May be ``None`` when no DAR-aware actions are
            present (validation skips agent checks).
        dry_run: True returns a :class:`DryRunResult` without
            persistence; ``approval_event_id`` is ignored.
        approval_event_id: REQUIRED when ``dry_run=False``. Bound to the
            new workflow row via the ``approval_event_id`` column;
            partial UNIQUE constraint enforces single-use.

    Returns:
        :class:`DryRunResult` when ``dry_run=True``; the persisted
        :class:`Workflow` otherwise.

    Raises:
        ApprovalBindingMissing: ``dry_run=False`` without approval.
        ApprovalEventNotFound, ApprovalEventTypeInvalid,
        ApprovalAuthoritySpoofed, ApprovalAuthorityIncomplete,
        ApprovalProvenanceUnverifiable, ApprovalProposalMismatch,
        ApprovalInstanceMismatch, ApprovalModificationTargetMismatch,
        ApprovalModificationTargetMissing: see approval.py.
        ApprovalDescriptorMismatch: recomputed hash != approval hash.
        RegistrationValidationFailed: revalidation produced errors.
        ApprovalAlreadyConsumed: TERMINAL — caller MUST NOT retry.
    """
    # Step 1: validate caller arguments.
    if not instance_id:
        raise ValueError("instance_id is required")
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"descriptor must be a dict, got {type(descriptor).__name__}"
        )
    # Cross-check the descriptor's own instance_id against the caller.
    # Without this guard, a caller in instance A could approve a
    # descriptor whose instance_id field is B and consume the approval
    # under B's (instance_id, approval_event_id) unique key. The
    # approval-event lookup is already instance-scoped (Step 2); this
    # closes the symmetric gap on the descriptor side.
    descriptor_instance = descriptor.get("instance_id", "")
    if descriptor_instance and descriptor_instance != instance_id:
        raise ApprovalInstanceMismatch(
            f"descriptor.instance_id={descriptor_instance!r} does not "
            f"match caller instance_id={instance_id!r}"
        )

    if dry_run:
        return await run_full_validation(
            descriptor, agent_registry=agent_registry,
        )

    # Real registration: must be approval-bound.
    if not approval_event_id:
        raise ApprovalBindingMissing(
            "register_workflow(dry_run=False) requires approval_event_id"
        )

    # Steps 2-5b: resolve and validate the approval event.
    approval_event = await resolve_and_validate_approval(
        instance_id=instance_id,
        approval_event_id=approval_event_id,
        descriptor=descriptor,
        workflow_registry=workflow_registry,
    )

    # Step 6: re-run full descriptor validation NOW (P7).
    # The dry-run output is NEVER cached — provider state may have
    # drifted between proposal and registration (provider disconnected,
    # agent retired, etc.). Full validation re-runs immediately.
    validation = await run_full_validation(
        descriptor, agent_registry=agent_registry,
    )

    # Step 7: hash match.
    if validation.descriptor_hash != approval_event.payload.get("descriptor_hash"):
        raise ApprovalDescriptorMismatch(
            f"recomputed descriptor_hash={validation.descriptor_hash!r} != "
            f"approval.descriptor_hash="
            f"{approval_event.payload.get('descriptor_hash')!r}"
        )

    # Step 8: verify validation passed.
    if not validation.valid:
        raise RegistrationValidationFailed(
            f"registration-time revalidation failed with "
            f"{len(validation.issues)} issue(s)",
            issues=list(validation.issues),
        )

    # Step 9: atomic persist + UNIQUE-constraint consumption.
    # Build the Workflow from the descriptor (we know it parses — Step 6
    # passed).
    from kernos.kernel.workflows.descriptor_parser import _build_workflow

    wf = _build_workflow(descriptor)
    try:
        registered = await workflow_registry._register_workflow_unbound(
            wf, approval_event_id=approval_event_id,
        )
    except aiosqlite.IntegrityError as exc:
        # The partial UNIQUE on (instance_id, approval_event_id) fired —
        # this approval has already been consumed.
        #
        # Hardening (Codex final-pass): match ONLY the approval-binding
        # index name. A duplicate workflow_id would also surface as
        # IntegrityError but is recoverable (the approval was rolled
        # back); translating that to ApprovalAlreadyConsumed would
        # incorrectly mark a recoverable failure as terminal. Re-raise
        # any other IntegrityError so the caller sees the underlying
        # constraint violation.
        # SQLite's IntegrityError message names the conflicting columns
        # rather than the index name, e.g. "UNIQUE constraint failed:
        # workflows.instance_id, workflows.approval_event_id". Match on
        # the column-pair signature so a duplicate workflow_id PK
        # (different shape) re-raises as the underlying IntegrityError
        # rather than mis-translating to terminal ApprovalAlreadyConsumed.
        msg = str(exc)
        is_approval_index_violation = (
            "idx_workflows_approval_unique" in msg
            or (
                "approval_event_id" in msg
                and "instance_id" in msg
                and "UNIQUE" in msg.upper()
            )
        )
        if is_approval_index_violation:
            raise ApprovalAlreadyConsumed(
                f"approval_event_id={approval_event_id!r} has already been "
                f"consumed in instance={instance_id!r}; this is a TERMINAL "
                f"failure mode — do NOT retry"
            ) from exc
        raise
    return registered


__all__ = ["register_workflow"]
