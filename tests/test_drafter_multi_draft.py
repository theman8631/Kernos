"""Multi-draft selection + multi-intent tests (DRAFTER C2, AC #14, #15, #16)."""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.multi_draft import (
    IntentCandidate,
    has_multi_intent,
    select_relevant_drafts,
)
from kernos.kernel.drafts.registry import WorkflowDraft


def _draft(
    *,
    draft_id: str = "d-1",
    instance_id: str = "inst_a",
    home_space_id: str | None = None,
    source_thread_id: str | None = None,
    status: str = "shaping",
    created_at: str = "2026-04-30T00:00:00+00:00",
) -> WorkflowDraft:
    return WorkflowDraft(
        draft_id=draft_id,
        instance_id=instance_id,
        home_space_id=home_space_id,
        source_thread_id=source_thread_id,
        status=status,
        created_at=created_at,
    )


# ===========================================================================
# AC #15 — context-scoped draft selection
# ===========================================================================


class TestContextScopedSelection:
    def test_selects_by_home_space_match(self):
        drafts = [
            _draft(draft_id="d-1", home_space_id="spc_general"),
            _draft(draft_id="d-2", home_space_id="spc_work"),
            _draft(draft_id="d-3", home_space_id="spc_general"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id="spc_general", source_thread_id=None,
        )
        assert {d.draft_id for d in result} == {"d-1", "d-3"}

    def test_selects_by_thread_match(self):
        drafts = [
            _draft(draft_id="d-1", source_thread_id="thr_a"),
            _draft(draft_id="d-2", source_thread_id="thr_b"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id=None, source_thread_id="thr_a",
        )
        assert {d.draft_id for d in result} == {"d-1"}

    def test_either_match_selects_or(self):
        """OR semantics: a draft matching either field is selected."""
        drafts = [
            _draft(
                draft_id="d-1", home_space_id="spc_x", source_thread_id="thr_y",
            ),
            _draft(
                draft_id="d-2", home_space_id="spc_other", source_thread_id="thr_y",
            ),
            _draft(
                draft_id="d-3", home_space_id="spc_x", source_thread_id="thr_other",
            ),
            _draft(
                draft_id="d-4", home_space_id="spc_other", source_thread_id="thr_other",
            ),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id="spc_x", source_thread_id="thr_y",
        )
        # d-1 / d-2 / d-3 all match (one or both fields). d-4 matches neither.
        assert {d.draft_id for d in result} == {"d-1", "d-2", "d-3"}

    def test_excludes_terminal_states(self):
        drafts = [
            _draft(draft_id="d-1", home_space_id="spc_x", status="shaping"),
            _draft(draft_id="d-2", home_space_id="spc_x", status="committed"),
            _draft(draft_id="d-3", home_space_id="spc_x", status="abandoned"),
            _draft(draft_id="d-4", home_space_id="spc_x", status="ready"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id="spc_x", source_thread_id=None,
        )
        assert {d.draft_id for d in result} == {"d-1", "d-4"}

    def test_no_context_no_match(self):
        """Both context fields None → no draft matches (no implicit
        catch-all)."""
        drafts = [
            _draft(draft_id="d-1", home_space_id="spc_x"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id=None, source_thread_id=None,
        )
        assert result == []

    def test_null_draft_field_does_not_implicit_match(self):
        """A draft with home_space_id=None doesn't accidentally match
        every space query."""
        drafts = [
            _draft(draft_id="d-1", home_space_id=None, source_thread_id=None),
            _draft(draft_id="d-2", home_space_id="spc_x"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id="spc_x", source_thread_id=None,
        )
        assert {d.draft_id for d in result} == {"d-2"}


# ===========================================================================
# AC #16 — oldest-first promotion order
# ===========================================================================


class TestOldestFirstOrder:
    def test_sorted_ascending_by_created_at(self):
        drafts = [
            _draft(draft_id="newest", home_space_id="spc",
                   created_at="2026-04-30T03:00:00+00:00"),
            _draft(draft_id="oldest", home_space_id="spc",
                   created_at="2026-04-30T01:00:00+00:00"),
            _draft(draft_id="middle", home_space_id="spc",
                   created_at="2026-04-30T02:00:00+00:00"),
        ]
        result = select_relevant_drafts(
            drafts, home_space_id="spc", source_thread_id=None,
        )
        assert [d.draft_id for d in result] == ["oldest", "middle", "newest"]


# ===========================================================================
# AC #14 — multi-intent detection
# ===========================================================================


class TestMultiIntentDetection:
    def test_two_or_more_strong_candidates_is_multi_intent(self):
        cands = [
            IntentCandidate(summary="set up A", confidence=0.8),
            IntentCandidate(summary="set up B", confidence=0.85),
        ]
        assert has_multi_intent(cands) is True

    def test_one_candidate_is_single_intent(self):
        cands = [IntentCandidate(summary="set up A", confidence=0.9)]
        assert has_multi_intent(cands) is False

    def test_empty_list_is_not_multi_intent(self):
        assert has_multi_intent([]) is False

    def test_intent_candidate_validates_confidence(self):
        with pytest.raises(ValueError):
            IntentCandidate(summary="x", confidence=1.5)
        with pytest.raises(ValueError):
            IntentCandidate(summary="x", confidence=-0.1)

    def test_intent_candidate_requires_summary(self):
        with pytest.raises(ValueError):
            IntentCandidate(summary="", confidence=0.8)
