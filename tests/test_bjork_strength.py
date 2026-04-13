"""Tests for Bjork dual-strength memory activation."""
import math
import pytest
from datetime import datetime, timezone, timedelta

from kernos.kernel.state import (
    KnowledgeEntry,
    compute_retrieval_strength,
    ARCHETYPE_STABILITY,
)


def _make_entry(
    archetype: str = "structural",
    storage_strength: float = 1.0,
    reinforcement_count: int = 1,
    days_ago: float = 0,
    last_reinforced: bool = True,
) -> KnowledgeEntry:
    now = datetime.now(timezone.utc)
    lr = (now - timedelta(days=days_ago)).isoformat() if last_reinforced else ""
    return KnowledgeEntry(
        id="test_entry",
        instance_id="t1",
        subject="user",
        content="test fact",
        category="fact",
        confidence="high",
        source_event_id="",
        source_description="test",
        last_referenced=now.isoformat(),
        tags=[],
        lifecycle_archetype=archetype,
        storage_strength=storage_strength,
        reinforcement_count=reinforcement_count,
        last_reinforced_at=lr,
        created_at=now.isoformat(),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestComputeRetrievalStrength:
    def test_new_entry_full_strength(self):
        """Entry with no last_reinforced_at returns 1.0."""
        e = _make_entry(last_reinforced=False)
        assert compute_retrieval_strength(e, _now_iso()) == 1.0

    def test_just_reinforced_full_strength(self):
        """Entry reinforced 0 days ago → ~1.0."""
        e = _make_entry(days_ago=0)
        assert compute_retrieval_strength(e, _now_iso()) >= 0.99

    def test_decay_over_time(self):
        """Strength decreases with time."""
        e1 = _make_entry(days_ago=1)
        e7 = _make_entry(days_ago=7)
        e30 = _make_entry(days_ago=30)
        s1 = compute_retrieval_strength(e1, _now_iso())
        s7 = compute_retrieval_strength(e7, _now_iso())
        s30 = compute_retrieval_strength(e30, _now_iso())
        assert s1 > s7 > s30

    def test_archetype_affects_decay_rate(self):
        """Identity decays slower than contextual."""
        e_id = _make_entry(archetype="identity", days_ago=30)
        e_ctx = _make_entry(archetype="contextual", days_ago=30)
        s_id = compute_retrieval_strength(e_id, _now_iso())
        s_ctx = compute_retrieval_strength(e_ctx, _now_iso())
        assert s_id > s_ctx  # Identity more stable

    def test_storage_strength_slows_decay(self):
        """Higher storage_strength → slower decay."""
        e_weak = _make_entry(storage_strength=1.0, days_ago=60)
        e_strong = _make_entry(storage_strength=10.0, days_ago=60)
        s_weak = compute_retrieval_strength(e_weak, _now_iso())
        s_strong = compute_retrieval_strength(e_strong, _now_iso())
        assert s_strong > s_weak

    def test_ephemeral_decays_fast(self):
        """Ephemeral entries should decay faster than structural."""
        e_eph = _make_entry(archetype="ephemeral", days_ago=7)
        e_str = _make_entry(archetype="structural", days_ago=7)
        s_eph = compute_retrieval_strength(e_eph, _now_iso())
        s_str = compute_retrieval_strength(e_str, _now_iso())
        assert s_eph < s_str  # Ephemeral decays faster

    def test_identity_stays_strong(self):
        """Identity entries stay accessible for months."""
        e = _make_entry(archetype="identity", days_ago=180)
        s = compute_retrieval_strength(e, _now_iso())
        assert s > 0.5  # Still accessible after 6 months

    def test_shellfish_allergy_scenario(self):
        """The motivating example: well-established fact, not recently accessed.

        Mentioned once 180 days ago. With storage_strength=1 (single mention),
        retrieval strength should be low-ish but not zero for structural archetype.
        With storage_strength=5 (reinforced 5 times), it should be notably higher.
        """
        e_once = _make_entry(archetype="structural", storage_strength=1.0, days_ago=180)
        e_reinforced = _make_entry(archetype="structural", storage_strength=5.0, days_ago=180)
        s_once = compute_retrieval_strength(e_once, _now_iso())
        s_reinforced = compute_retrieval_strength(e_reinforced, _now_iso())
        assert s_reinforced > s_once
        assert s_reinforced > 0.2  # Still accessible when well-established


class TestCandidateRanking:
    """Tests for the ranking behavior in handler (integration-level)."""

    def test_very_old_ephemeral_below_threshold(self):
        """Very old ephemeral entries fall below the 0.10 filter threshold."""
        e = _make_entry(archetype="ephemeral", days_ago=500)
        s = compute_retrieval_strength(e, _now_iso())
        assert s < 0.10

    def test_contextual_14_days_still_accessible(self):
        """Contextual entry at 14 days should still have measurable strength.

        The old _is_stale_knowledge(days=14) would have killed this.
        The Bjork model keeps it alive if storage_strength is decent.
        """
        e = _make_entry(archetype="contextual", storage_strength=3.0, days_ago=14)
        s = compute_retrieval_strength(e, _now_iso())
        assert s > 0.1  # Still accessible with reinforcement


class TestArchetypeStability:
    def test_identity_longest(self):
        assert ARCHETYPE_STABILITY["identity"] > ARCHETYPE_STABILITY["structural"]

    def test_ephemeral_shortest(self):
        assert ARCHETYPE_STABILITY["ephemeral"] < ARCHETYPE_STABILITY["contextual"]

    def test_all_archetypes_present(self):
        for arch in ["identity", "structural", "habitual", "contextual", "ephemeral"]:
            assert arch in ARCHETYPE_STABILITY
