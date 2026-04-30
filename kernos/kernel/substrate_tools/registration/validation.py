"""Dry-run + descriptor validation assembly for STS.

The dry-run path (``register_workflow(dry_run=True)``) produces a
:class:`DryRunResult` that the Compiler can show to the user before
committing to an approval. The same validation pipeline runs again at
registration time (P7); the dry-run is NEVER a cacheable permission
slip — a value that was valid at proposal time may have become invalid
between then and registration (provider disconnected, agent retired,
etc.).

Validation steps (in order):

1. Compute canonical descriptor hash.
2. Build a :class:`Workflow` from the descriptor dict (catches structural
   parse errors as Issues with ``code="descriptor_parse_error"``).
3. Run :func:`validate_workflow` (catches structural workflow errors as
   Issues with ``code="workflow_validation"``).
4. Validate route-to-agent references against DAR (catches unknown /
   paused / retired agents as Issues with ``code="unknown_agent"`` /
   ``"agent_not_active"``).

Capability gaps (provider-related) are surfaced as
:class:`CapabilityGap` rather than Issues. v1's gap detection is
conservative: it does not yet probe `ProviderRegistry` for missing
capabilities — that is the Drafter cohort's job. Empty capability_gaps
in v1 means "we did not detect any" rather than "guaranteed no gaps."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kernos.kernel.substrate_tools.query.list_providers import (
    CapabilityGap,
    Issue,
)
from kernos.kernel.substrate_tools.registration.descriptor_hash import (
    compute_descriptor_hash,
)

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRegistry
    from kernos.kernel.workflows.workflow_registry import Workflow


@dataclass(frozen=True)
class DryRunResult:
    """The shape returned from ``register_workflow(dry_run=True)``.

    ``valid`` is True if and only if no error-severity Issue or
    CapabilityGap was produced. Warnings and info do not gate
    validity."""

    valid: bool
    descriptor_hash: str
    issues: tuple[Issue, ...] = ()
    capability_gaps: tuple[CapabilityGap, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.issues, list):
            object.__setattr__(self, "issues", tuple(self.issues))
        if isinstance(self.capability_gaps, list):
            object.__setattr__(self, "capability_gaps", tuple(self.capability_gaps))


async def run_full_validation(
    descriptor: dict,
    *,
    agent_registry: "AgentRegistry | None" = None,
) -> DryRunResult:
    """Run the full validation pipeline against a descriptor.

    Same path used by both dry-run (Step 5 of dry-run flow) and real
    registration (Step 6 of production flow). Pure: no persistence,
    no event emission, no mutation.
    """
    from kernos.kernel.workflows.descriptor_parser import (
        DescriptorError,
        _build_workflow,
    )
    from kernos.kernel.workflows.workflow_registry import (
        WorkflowError,
        validate_workflow,
    )

    descriptor_hash = compute_descriptor_hash(descriptor)
    issues: list[Issue] = []
    capability_gaps: list[CapabilityGap] = []

    # Step 2: descriptor → Workflow.
    wf: "Workflow | None" = None
    try:
        wf = _build_workflow(descriptor)
    except DescriptorError as exc:
        issues.append(Issue(
            severity="error",
            code="descriptor_parse_error",
            message=str(exc),
        ))
    except Exception as exc:  # noqa: BLE001 — surface unexpected as Issue
        issues.append(Issue(
            severity="error",
            code="descriptor_parse_error",
            message=f"unexpected descriptor parse error: {exc}",
        ))

    # Step 3: structural workflow validation.
    if wf is not None:
        try:
            validate_workflow(wf)
        except WorkflowError as exc:
            issues.append(Issue(
                severity="error",
                code="workflow_validation",
                message=str(exc),
            ))

    # Step 4: route-to-agent reference resolution against DAR.
    if wf is not None and agent_registry is not None:
        for idx, action in enumerate(wf.action_sequence):
            if action.action_type != "route_to_agent":
                continue
            agent_id = action.parameters.get("agent_id", "") or ""
            if not isinstance(agent_id, str):
                issues.append(Issue(
                    severity="error",
                    code="unknown_agent",
                    message=(
                        f"action_sequence[{idx}].parameters.agent_id "
                        f"must be a string"
                    ),
                    path=f"action_sequence[{idx}].parameters.agent_id",
                ))
                continue
            if not agent_id or agent_id.startswith("@default:"):
                # Default-agent references are conversational-only and
                # are validated at dispatch time, not at registration.
                continue
            rec = await agent_registry.get_by_id(agent_id, wf.instance_id)
            if rec is None:
                issues.append(Issue(
                    severity="error",
                    code="unknown_agent",
                    message=(
                        f"action_sequence[{idx}] references unregistered "
                        f"agent_id={agent_id!r}"
                    ),
                    path=f"action_sequence[{idx}].parameters.agent_id",
                ))
            elif rec.status != "active":
                issues.append(Issue(
                    severity="error",
                    code="agent_not_active",
                    message=(
                        f"action_sequence[{idx}] references agent "
                        f"{agent_id!r} with status={rec.status!r}"
                    ),
                    path=f"action_sequence[{idx}].parameters.agent_id",
                    metadata={"agent_status": rec.status},
                ))

    valid = not any(
        i.severity == "error" for i in issues
    ) and not any(
        g.severity == "error" for g in capability_gaps
    )
    return DryRunResult(
        valid=valid,
        descriptor_hash=descriptor_hash,
        issues=tuple(issues),
        capability_gaps=tuple(capability_gaps),
    )


__all__ = ["DryRunResult", "run_full_validation"]
