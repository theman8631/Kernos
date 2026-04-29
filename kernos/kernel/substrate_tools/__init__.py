"""Substrate Tools (STS) — cohort-facing facade over WDP/DAR/WLP/provider config.

Future-composition invariant: STS exposes stable refs, capability summaries,
validation results, and approval-bound registration WITHOUT depending directly
on Canvas, domains, tools, agents, or unknown future systems. Canvas, domain
briefs, provider config, and future surfaces plug in as context/capability
providers behind neutral query APIs. STS is a small deterministic facade over
discoverable substrate; not a growing switchboard that learns every surface
by name. Reviewers of follow-on specs should reject changes that introduce
direct subsystem dependencies into STS.

Module shape:

* :class:`SubstrateTools` (facade) — cohort entry point. C1: query
  surfaces only. C2: adds approval-bound :meth:`register_workflow`.
* ``query/`` — read surfaces: list_providers, list_agents, list_workflows,
  list_drafts, query_context_brief.
* ``registration/`` — descriptor hash, dry-run validation, approval
  validation, registration gate. (C2.)
* :mod:`kernos.kernel.substrate_tools.errors` — typed error hierarchy.
"""
from __future__ import annotations

from kernos.kernel.substrate_tools.facade import SubstrateTools
from kernos.kernel.substrate_tools.query.context_brief import (
    ContextBrief,
    ContextBriefRegistry,
    ContextRef,
)
from kernos.kernel.substrate_tools.query.list_providers import (
    CapabilityGap,
    InvalidCapabilityTagFormat,
    Issue,
    ProviderRecord,
    ProviderRegistry,
    validate_capability_tag,
)
from kernos.kernel.substrate_tools.errors import SubstrateToolsError

__all__ = [
    "CapabilityGap",
    "ContextBrief",
    "ContextBriefRegistry",
    "ContextRef",
    "InvalidCapabilityTagFormat",
    "Issue",
    "ProviderRecord",
    "ProviderRegistry",
    "SubstrateTools",
    "SubstrateToolsError",
    "validate_capability_tag",
]
