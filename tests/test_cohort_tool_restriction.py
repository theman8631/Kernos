"""Universal cohort tool-restriction substrate tests (DRAFTER C1, AC #9).

The :class:`CohortToolWhitelist` substrate is reusable across system
cohorts. Drafter is the first user; future Pattern Observer / Curator
cohorts inherit the same shape. Tests here pin the universal behavior;
Drafter-specific pins live in test_drafter_ports.py.
"""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts._substrate.tool_restriction import (
    CohortToolForbidden,
    CohortToolWhitelist,
)


class _FakeCohortToolForbidden(CohortToolForbidden):
    """Subclass simulating a future cohort's typed alias."""


class TestUniversalSubstrate:
    def test_construct_with_frozenset(self):
        wl = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=frozenset({"DraftRegistry.create_draft"}),
        )
        assert wl.cohort_name == "drafter"
        assert wl.allowed_tools == frozenset({"DraftRegistry.create_draft"})

    def test_mutable_set_rejected(self):
        with pytest.raises(TypeError, match="frozenset"):
            CohortToolWhitelist(
                cohort_name="drafter",
                allowed_tools={"a"},  # type: ignore[arg-type]
            )

    def test_empty_cohort_name_rejected(self):
        with pytest.raises(ValueError):
            CohortToolWhitelist(
                cohort_name="",
                allowed_tools=frozenset({"a"}),
            )

    def test_check_allowed_tool_returns_none(self):
        wl = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=frozenset({"DraftRegistry.create_draft"}),
        )
        # No raise.
        assert wl.check(tool_name="DraftRegistry.create_draft") is None

    def test_check_forbidden_tool_raises_default(self):
        wl = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=frozenset({"DraftRegistry.create_draft"}),
        )
        with pytest.raises(CohortToolForbidden) as exc_info:
            wl.check(tool_name="DraftRegistry.mark_committed")
        assert "drafter" in str(exc_info.value)
        assert "mark_committed" in str(exc_info.value)


class TestTypedAliasSupport:
    """Future cohorts (Pattern Observer, Curator) define typed aliases
    that subclass :class:`CohortToolForbidden`. The substrate raises the
    cohort's typed alias so test pins can distinguish failure source."""

    def test_typed_alias_raised(self):
        wl = CohortToolWhitelist(
            cohort_name="future_cohort",
            allowed_tools=frozenset({"a"}),
            forbidden_exception=_FakeCohortToolForbidden,
        )
        with pytest.raises(_FakeCohortToolForbidden):
            wl.check(tool_name="forbidden")

    def test_typed_alias_is_cohort_tool_forbidden_subclass(self):
        # Typed aliases must subclass the universal exception so generic
        # handlers can catch all cohort restriction violations.
        wl = CohortToolWhitelist(
            cohort_name="future_cohort",
            allowed_tools=frozenset({"a"}),
            forbidden_exception=_FakeCohortToolForbidden,
        )
        try:
            wl.check(tool_name="forbidden")
        except CohortToolForbidden:
            pass  # Subclass IS-A CohortToolForbidden — clean.
        else:
            pytest.fail("expected CohortToolForbidden subclass to fire")


class TestIsAllowed:
    """Pure check (no raise) — used by callers that want to branch."""

    def test_is_allowed_returns_true_for_whitelisted(self):
        wl = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=frozenset({"a", "b"}),
        )
        assert wl.is_allowed(tool_name="a") is True

    def test_is_allowed_returns_false_for_non_whitelisted(self):
        wl = CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=frozenset({"a", "b"}),
        )
        assert wl.is_allowed(tool_name="c") is False
