"""CRB approval flow.

* :mod:`flow` — :class:`CRBApprovalFlow` state machine. Owns
  ``handle_response`` (with crash-safe approval-to-registration
  handoff), ``handle_explicit_modification_request`` fallback,
  ``handle_disambiguation_response`` with permission gate, and the
  ``recover_pending_registrations`` engine-startup sweep.
* :mod:`duplicate_handling` — six duplicate / late approval cases
  (Seam C11).
* :mod:`ports` — typed Protocols the flow takes as dependencies.
  Restricted-port pattern preserved so tests inject deterministic
  stubs.
"""
from __future__ import annotations
