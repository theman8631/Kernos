"""Conversational Routine Builder (CRB) — service module.

The user-facing surface where conversations become installed routines.
After Drafter shapes a draft and STS validates it, CRB authors the
proposal, owns the approval flow state machine, and emits the
approval events that STS consumes for real registration.

CRB is **a service module the principal cohort uses**, NOT a cohort.
No independent cursor, no budget, no passive observation. Reactive
only when the principal invokes CRB's authoring or approval methods.
The principal cohort decides WHEN to surface things; CRB decides
WHAT to say.

Anti-fragmentation invariant: CRB consumes shared context surfaces
(event_stream, WDP DraftRegistry, STS query surfaces, principal
cohort context). CRB does NOT build a parallel context model. When
the shared-situation-model spec lands post-CRB, CRB slots in by
consuming the new shared object without internal rework. Reviewers
should reject changes that introduce CRB-private context state,
parallel friction-detection logic, or shadow registries.

Future-composition invariant: CRB stays a service module. Future
specs that compose against CRB (Workshop layer, voice/expression,
audit log surface, routine library) consume CRB's existing facades
without converting CRB into a cohort. Reviewers should reject
changes that introduce a cursor, budget, or passive observation
loop into CRB.

Module layout:

* :mod:`compiler` — pure descriptor translation
  (``draft_to_descriptor_candidate``) + cheap shape assertions.
  Replaces Drafter v1's compiler_helper_stub.
* :mod:`proposal` — InstallProposal types, durable
  ``InstallProposalStore``, and the LLM-driven ``CRBProposalAuthor``.
* :mod:`approval` — ``CRBApprovalFlow`` state machine + duplicate /
  late approval handling + crash-recovery sweep.
* :mod:`events` — emitter registration + emit helpers for
  ``routine.proposed``, ``routine.approved``,
  ``routine.modification.approved``, ``routine.declined``,
  ``crb.feedback.modify_request``.
* :mod:`principal_integration` — subscription and receipt-ack wiring.
* :mod:`errors` — typed error hierarchy.
"""
from __future__ import annotations

from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.errors import (
    CRBError,
    CompilerError,
    DraftSchemaIncomplete,
    DraftShapeMalformed,
)


__all__ = [
    "CRBError",
    "CompilerError",
    "DraftSchemaIncomplete",
    "DraftShapeMalformed",
    "draft_to_descriptor_candidate",
]
