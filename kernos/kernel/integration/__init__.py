"""Integration layer.

Sits between cohorts and presence in the four-layer cognition
architecture (cohorts → integration → presence → expression-future).
This package ships the foundational primitive: the briefing
schema and the integration runner. Subsequent specs build the
per-turn cohort fan-out runner, the cohort adapters, and
presence decoupling against the contract this package defines.

The runner is opt-in callable (spec acceptance criterion #13) —
nothing in the existing reasoning loop calls it yet.

The briefing is the architecture's safety surface: redaction at
the briefing boundary keeps Restricted CohortOutput content from
leaking into presence even though integration may use it to
shape the decision.
"""

from kernos.kernel.integration.briefing import (
    ActionKind,
    AuditTrace,
    Briefing,
    BriefingValidationError,
    BudgetState,
    CohortOutput,
    ConstrainedResponse,
    ContextItem,
    DecidedAction,
    Defer,
    ExecuteTool,
    FilteredItem,
    Pivot,
    ProposeTool,
    Public,
    RespondOnly,
    Restricted,
    Visibility,
    VisibilityKind,
    decided_action_from_dict,
    minimal_fail_soft_briefing,
    now_iso,
    visibility_from_dict,
)

__all__ = [
    "ActionKind",
    "AuditTrace",
    "Briefing",
    "BriefingValidationError",
    "BudgetState",
    "CohortOutput",
    "ConstrainedResponse",
    "ContextItem",
    "DecidedAction",
    "Defer",
    "ExecuteTool",
    "FilteredItem",
    "Pivot",
    "ProposeTool",
    "Public",
    "RespondOnly",
    "Restricted",
    "Visibility",
    "VisibilityKind",
    "decided_action_from_dict",
    "minimal_fail_soft_briefing",
    "now_iso",
    "visibility_from_dict",
]
