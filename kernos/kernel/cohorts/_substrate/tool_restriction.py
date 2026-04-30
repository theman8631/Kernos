"""Universal cohort tool restriction (Drafter D4 + reusable substrate).

Belt-and-suspenders enforcement for tool-starved cohorts. The primary
trust boundary is structural: cohorts receive restricted port facades
(e.g. :class:`DrafterDraftPort`) that have forbidden methods STRUCTURALLY
ABSENT from the surface. The whitelist here catches any escape path —
serialization reconstitution, reflection, code that bypasses the port
surface — by verifying tool dispatch against an allowed set.

Drafter is the first user; future Pattern Observer / Curator cohorts
declare their own whitelists in the same shape.
"""
from __future__ import annotations


class CohortToolForbidden(Exception):
    """Raised when a system cohort attempts a non-whitelisted tool dispatch.

    Subclasses provide cohort-specific typed aliases so test pins can
    distinguish cohorts (e.g. ``DrafterToolForbidden``)."""


class CohortToolWhitelist:
    """Universal whitelist enforcement at the cohort substrate.

    Construction takes the cohort name plus the frozenset of allowed
    tool names. ``check`` raises :class:`CohortToolForbidden` (or a
    cohort-specific subclass passed via ``forbidden_exception``) when
    a dispatch attempt names a tool not in the allowed set.

    Usage at engine bring-up::

        whitelist = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=DRAFTER_WHITELIST,
            forbidden_exception=DrafterToolForbidden,
        )
        whitelist.check(tool_name="DraftRegistry.create_draft")  # ok
        whitelist.check(tool_name="DraftRegistry.mark_committed")  # raises

    The allowed-tools set is frozen for the lifetime of the registry
    instance — tool restriction is a deploy-time invariant, not runtime
    state.
    """

    def __init__(
        self,
        *,
        cohort_name: str,
        allowed_tools: frozenset[str],
        forbidden_exception: type[CohortToolForbidden] = CohortToolForbidden,
    ) -> None:
        if not cohort_name:
            raise ValueError("cohort_name is required")
        if not isinstance(allowed_tools, frozenset):
            raise TypeError(
                "allowed_tools must be a frozenset (immutable for the "
                "lifetime of the registry instance)"
            )
        self._cohort_name = cohort_name
        self._allowed_tools = allowed_tools
        self._forbidden_exception = forbidden_exception

    @property
    def cohort_name(self) -> str:
        return self._cohort_name

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self._allowed_tools

    def is_allowed(self, *, tool_name: str) -> bool:
        return tool_name in self._allowed_tools

    def check(self, *, tool_name: str) -> None:
        """Raise the configured forbidden exception if ``tool_name`` is
        not in the allowed set. Returns ``None`` on success."""
        if tool_name not in self._allowed_tools:
            raise self._forbidden_exception(
                f"cohort {self._cohort_name!r} attempted non-whitelisted "
                f"tool dispatch: {tool_name!r}. Allowed tools: "
                f"{sorted(self._allowed_tools)}"
            )


__all__ = ["CohortToolForbidden", "CohortToolWhitelist"]
