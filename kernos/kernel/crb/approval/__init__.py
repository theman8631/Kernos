"""CRB approval flow.

* :mod:`flow` — :class:`CRBApprovalFlow` state machine. Owns
  ``handle_response`` (with crash-safe approval-to-registration
  handoff and the six duplicate/late C11 cases inline),
  ``handle_explicit_modification_request`` fallback,
  ``handle_disambiguation_response`` with permission gate, and the
  ``recover_pending_registrations`` engine-startup sweep.
* :mod:`ports` — typed Protocols the flow takes as dependencies.
  Restricted-port pattern preserved so tests inject deterministic
  stubs.

Elegance latitude (deviation from spec): the spec called for a
separate ``duplicate_handling.py`` module for the C11 cases. v1
keeps them inline in ``handle_response`` because they're branch
logic specific to that one method; a separate module would add
indirection without reuse.
"""
from __future__ import annotations

from kernos.kernel.crb.approval.flow import (
    ApprovalFlowError,
    CRBApprovalFlow,
)
from kernos.kernel.crb.approval.ports import (
    CRBEventPort,
    DraftReadPort,
    STSRegistrationPort,
    STSTransientError,
)


__all__ = [
    "ApprovalFlowError",
    "CRBApprovalFlow",
    "CRBEventPort",
    "DraftReadPort",
    "STSRegistrationPort",
    "STSTransientError",
]
