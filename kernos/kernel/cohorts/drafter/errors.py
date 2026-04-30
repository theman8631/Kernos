"""Drafter error hierarchy."""
from __future__ import annotations

from kernos.kernel.cohorts._substrate.tool_restriction import (
    CohortToolForbidden,
)


class DrafterError(Exception):
    """Base for all Drafter-raised errors."""


class DrafterToolForbidden(CohortToolForbidden, DrafterError):
    """Drafter-specific tool restriction violation. Subclass of the
    universal :class:`CohortToolForbidden` so the substrate-level
    whitelist can raise the typed alias for diagnostics."""


class DrafterBudgetExhausted(DrafterError):
    """Raised when Tier 2 evaluation requested but per-instance budget
    exhausted within the current time window."""


class DrafterCursorCorruption(DrafterError):
    """Raised when CursorStore returns inconsistent or invalid cursor
    state."""


class DrafterReceiptTimeout(DrafterError):
    """Raised by the diagnostic check when a signal acknowledgment
    doesn't arrive within the configured threshold (default 60s,
    substrate-delivery level — NOT principal-speaks-to-user latency)."""


class DrafterDraftCreationUnauthorized(DrafterError):
    """Raised internally if persistent draft creation is attempted
    without ``permission_to_make_durable=True`` in the recognition
    evaluation. Should never reach test scenarios in production; pin
    guards against logic-bug regression."""


class DrafterCompilerHelperUnavailable(DrafterError):
    """Raised when the ``draft_to_descriptor_candidate`` helper is not
    configured. Drafter does NOT translate descriptors itself; the
    helper is required."""


__all__ = [
    "CohortToolForbidden",
    "DrafterBudgetExhausted",
    "DrafterCompilerHelperUnavailable",
    "DrafterCursorCorruption",
    "DrafterDraftCreationUnauthorized",
    "DrafterError",
    "DrafterReceiptTimeout",
    "DrafterToolForbidden",
]
