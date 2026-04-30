"""Drafter cohort — first tool-starved system cohort (DRAFTER v2 spec).

Drafter observes conversation events passively, holds workflow drafts
in scratch state via WDP, validates them via STS dry-run, and signals
the principal cohort when a draft becomes committable. Drafter NEVER
speaks to the user, NEVER auto-activates, NEVER calls real registration.

Anti-fragmentation invariant: Drafter consumes shared context surfaces
(event_stream, friction observer signals, WDP DraftRegistry, STS query
surfaces, principal cohort context). Drafter does NOT build a parallel
cohort-specific context model. When the shared-situation-model spec
lands post-CRB, Drafter slots in by consuming the new shared object
without internal rework. Reviewers should reject changes that introduce
Drafter-private context state, parallel friction-detection logic, or
shadow registries.

Future-composition invariant: Drafter is the first tool-starved cohort.
The universal cohort substrate at ``kernos/kernel/cohorts/_substrate/``
enforces tool restriction, durable cursor pull, and crash-idempotent
action recording generically; future Pattern Observer and Curator
cohorts inherit the same patterns without touching Drafter-specific
code.

Module layout:

* :mod:`cohort` — :class:`DrafterCohort` (lifecycle, tick-driven loop)
* :mod:`ports` — restricted port facades (capability wrappers with
  forbidden methods STRUCTURALLY ABSENT from the surface)
* :mod:`errors` — typed error hierarchy
* (C2) :mod:`evaluation`, :mod:`recognition`, :mod:`multi_draft`,
  :mod:`budget`
* (C3) :mod:`signals`, :mod:`receipts`, :mod:`compiler_helper_stub`
"""
from __future__ import annotations

from kernos.kernel.cohorts.drafter.errors import (
    DrafterCompilerHelperUnavailable,
    DrafterCursorCorruption,
    DrafterDraftCreationUnauthorized,
    DrafterError,
    DrafterReceiptTimeout,
    DrafterToolForbidden,
)
from kernos.kernel.cohorts.drafter.ports import (
    DRAFTER_WHITELIST,
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)


COHORT_ID = "drafter"
"""The canonical cohort identifier. Used for action_log scoping, cursor
scoping, and EmitterRegistry source_module identity."""


SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset({
    "conversation.message.posted",
    "conversation.context.shifted",
    "friction.signal.surfaced",
    # v1.1: inbound CRB feedback for draft re-shaping during proposal
    # review. Substrate-set envelope.source_module="crb" is the trust
    # boundary; payload-claimed source ignored.
    "crb.feedback.modify_request",
})
"""Event types Drafter pulls via the durable cursor. Other event types
are not delivered (cursor advances past them without invoking the
handler)."""


__all__ = [
    "COHORT_ID",
    "DRAFTER_WHITELIST",
    "DrafterCompilerHelperUnavailable",
    "DrafterCursorCorruption",
    "DrafterDraftCreationUnauthorized",
    "DrafterDraftPort",
    "DrafterError",
    "DrafterEventPort",
    "DrafterReceiptTimeout",
    "DrafterSubstrateToolsPort",
    "DrafterToolForbidden",
    "SUBSCRIBED_EVENT_TYPES",
]
