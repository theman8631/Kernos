"""CRB proposal substrate.

* :mod:`install_proposal` — :class:`InstallProposal` /
  :class:`FlowResponse` / :class:`FlowOutcome` types + the
  :data:`ProposalState` / :data:`ResponseKind` literals + the
  :class:`CandidateIntent` mirror imported from Drafter v1.2.
* :mod:`install_proposal_store` — SQLite-backed durable store
  (composite uniqueness on ``(instance_id, correlation_id)``,
  state-machine validation at the transition boundary).
* :mod:`author` (C3) — :class:`CRBProposalAuthor` LLM-driven
  authoring methods.
"""
from __future__ import annotations

from kernos.kernel.crb.proposal.install_proposal import (
    AmbiguityKind,
    DisambiguationOutcome,
    ExplicitModificationOutcome,
    FlowOutcome,
    FlowResponse,
    InstallProposal,
    PermittedTransitions,
    ProposalState,
    ResponseKind,
    is_terminal_state,
)
from kernos.kernel.crb.proposal.install_proposal_store import (
    DuplicateProposalCorrelation,
    InstallProposalStore,
    InvalidStateTransition,
    UnknownProposal,
)


__all__ = [
    "AmbiguityKind",
    "DisambiguationOutcome",
    "DuplicateProposalCorrelation",
    "ExplicitModificationOutcome",
    "FlowOutcome",
    "FlowResponse",
    "InstallProposal",
    "InstallProposalStore",
    "InvalidStateTransition",
    "PermittedTransitions",
    "ProposalState",
    "ResponseKind",
    "UnknownProposal",
    "is_terminal_state",
]
