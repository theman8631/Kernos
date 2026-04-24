"""CANVAS-SECTION-MARKERS + GARDENER Pillar 2 — cohort primitive scaffolding.

Tests the pure helpers + service-level state (pattern cache,
coalescer) without firing live LLM consultations. Pillar 3 and 4
tests exercise the actual judgment paths with stub reasoning services.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from kernos.cohorts.gardener import (
    EvolutionContext,
    GardenerDecision,
    GardenerExhausted,
    InitialShapeContext,
    SectionContext,
    _parse_decision,
    judge_evolution,
    judge_initial_shape,
)
from kernos.cohorts.gardener_prompts import (
    build_evolution_prompt,
    build_initial_shape_prompt,
    build_section_prompt,
)
from kernos.kernel.canvas import CanvasService
from kernos.kernel.gardener import (
    DEFAULT_COALESCE_MINUTES,
    GARDENER_SOURCE,
    GardenerService,
    PatternCache,
    PendingProposal,
    ProposalCoalescer,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_gardenertest"
OPERATOR = "member:inst_gardenertest:owner"


# ---- Pure: decision parsing + surfacing ------------------------------------


def test_parse_decision_accepts_high_confidence_action():
    raw = json.dumps({
        "action": "pick_pattern", "confidence": "high",
        "pattern": "software-development", "rationale": "clear dial match",
    })
    d = _parse_decision(raw)
    assert d is not None
    assert d.action == "pick_pattern"
    assert d.confidence == "high"
    assert d.surfaces is True


def test_parse_decision_none_action_never_surfaces():
    raw = json.dumps({"action": "none", "confidence": "high"})
    d = _parse_decision(raw)
    assert d.action == "none"
    assert d.surfaces is False


def test_parse_decision_low_confidence_does_not_surface():
    raw = json.dumps({"action": "propose_merge", "confidence": "low"})
    d = _parse_decision(raw)
    assert d.surfaces is False


def test_parse_decision_medium_confidence_does_not_surface():
    raw = json.dumps({"action": "propose_split", "confidence": "medium"})
    d = _parse_decision(raw)
    assert d.surfaces is False


def test_parse_decision_malformed_json_returns_none():
    assert _parse_decision("not-json") is None


def test_parse_decision_unknown_confidence_defaults_to_low():
    raw = json.dumps({"action": "propose_merge", "confidence": "uncertain"})
    d = _parse_decision(raw)
    assert d.confidence == "low"
    assert not d.surfaces


# ---- PatternCache ----------------------------------------------------------


def test_pattern_cache_invalidation_on_workflow_canvas():
    cache = PatternCache()
    cache.set_workflow_canvas_id("canvas_wp")
    cache.put("software-development", "body", {"pattern": "software-development"})
    cache.mark_loaded()
    assert cache.loaded
    # Non-matching canvas id: no invalidation
    assert not cache.invalidate_if_workflow("canvas_other")
    assert cache.loaded
    # Matching: invalidates
    assert cache.invalidate_if_workflow("canvas_wp")
    assert not cache.loaded


def test_pattern_cache_all_summaries_excludes_meta():
    cache = PatternCache()
    cache.put("library-meta", "meta body", {"pattern": "library-meta"})
    cache.put("software-development", "sd body", {"pattern": "software-development"})
    summaries = cache.all_summaries()
    names = {s["name"] for s in summaries}
    assert "software-development" in names
    assert "library-meta" not in names


def test_pattern_cache_cross_pattern_returns_meta_body():
    cache = PatternCache()
    cache.put("library-meta", "cross-pattern heuristics text", {})
    assert "cross-pattern" in cache.cross_pattern_body()


# ---- ProposalCoalescer -----------------------------------------------------


def test_coalescer_default_window_is_24h():
    c = ProposalCoalescer()
    assert c.window == timedelta(minutes=DEFAULT_COALESCE_MINUTES)


def test_coalescer_first_should_surface_is_true():
    c = ProposalCoalescer()
    assert c.should_surface("canvas_a") is True


def test_coalescer_buffers_multiple_proposals_per_canvas():
    c = ProposalCoalescer()
    now = datetime.now(timezone.utc)
    for i in range(3):
        c.add(PendingProposal(
            canvas_id="canvas_a", action="propose_merge",
            confidence="high", rationale=f"r{i}", affected_pages=[],
            captured_at=now,
        ))
    assert c.buffered_count("canvas_a") == 3


def test_coalescer_drain_empties_buffer_and_marks_window():
    c = ProposalCoalescer(window=timedelta(minutes=5))
    now = datetime.now(timezone.utc)
    c.add(PendingProposal(
        canvas_id="canvas_a", action="propose_merge",
        confidence="high", rationale="r", affected_pages=[],
        captured_at=now,
    ))
    drained = c.drain("canvas_a", now=now)
    assert len(drained) == 1
    assert c.buffered_count("canvas_a") == 0
    # Within window — should not surface again.
    assert c.should_surface("canvas_a", now=now + timedelta(minutes=1)) is False
    # After window elapses.
    assert c.should_surface("canvas_a", now=now + timedelta(minutes=10)) is True


def test_coalescer_per_canvas_isolation():
    c = ProposalCoalescer(window=timedelta(minutes=5))
    now = datetime.now(timezone.utc)
    c.add(PendingProposal(
        canvas_id="canvas_a", action="propose_merge", confidence="high",
        rationale="", affected_pages=[], captured_at=now,
    ))
    # Different canvas — independent window state.
    assert c.buffered_count("canvas_b") == 0
    assert c.should_surface("canvas_b") is True


# ---- Prompt builders (sanity — no LLM) -------------------------------------


def test_initial_shape_prompt_mentions_intent_and_patterns():
    ctx = InitialShapeContext(
        instance_id="i", canvas_id="c", canvas_name="My D&D",
        scope="team", creator_member_id="alice",
        intent="long-form campaign with party of five",
        available_patterns=[
            {"name": "long-form-campaign", "summary": "Narrative accumulation."},
            {"name": "software-development", "summary": "Build-with-shipping."},
        ],
    )
    system, user = build_initial_shape_prompt(ctx)
    assert "Gardener" in system
    assert "long-form campaign" in user
    assert "long-form-campaign" in user
    assert "software-development" in user


def test_evolution_prompt_includes_pattern_and_pages():
    ctx = EvolutionContext(
        instance_id="i", canvas_id="c",
        canvas_pattern="long-form-campaign",
        event_type="canvas.page.changed",
        page_path="sessions/s04.md",
        page_summary="Session 4 summary.",
        canvas_pages_index=[{"path": "sessions/s04.md", "type": "log"}],
        cross_pattern_heuristics="cross rules here",
    )
    _, user = build_evolution_prompt(ctx)
    assert "canvas.page.changed" in user
    assert "long-form-campaign" in user
    assert "sessions/s04.md" in user
    assert "cross rules here" in user


def test_section_prompt_includes_body_excerpt():
    ctx = SectionContext(
        instance_id="i", canvas_id="c",
        page_path="world.md", section_slug="sillverglass",
        section_heading="Sillverglass",
        section_body="Body text here.", current_marker_summary="old",
        current_marker_tokens=10,
    )
    _, user = build_section_prompt(ctx)
    assert "sillverglass" in user
    assert "Body text here" in user


# ---- GardenerService integration (no LLM) ----------------------------------


class _StubReasoning:
    """Never actually called in Pillar 2 tests."""
    async def complete_simple(self, *a, **kw):
        raise RuntimeError("Pillar 2 tests must not hit the LLM path")


@pytest.fixture
async def gardener_env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb,
        reasoning_service=_StubReasoning(),
    )
    yield gardener, svc, idb, tmp_path
    await gardener.wait_idle()
    await idb.close()


async def test_service_constructs_with_no_errors(gardener_env):
    gardener, _, _, _ = gardener_env
    assert gardener.coalescer is not None
    assert gardener.patterns is not None


async def test_service_ignores_its_own_events(gardener_env):
    """Gardener skips events tagged ``source: gardener`` (Hazard B — cascading reshapes)."""
    gardener, _, _, _ = gardener_env
    await gardener.on_canvas_event(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": "c1", "source": GARDENER_SOURCE},
    )
    await gardener.wait_idle()
    # No patterns should have been loaded (the short-circuit fires before).
    assert gardener.patterns.loaded is False


async def test_service_non_gardener_events_schedule_dispatch(gardener_env):
    """Non-Gardener events schedule background dispatch (doesn't raise)."""
    gardener, _, _, _ = gardener_env
    # Emit a non-canvas-specific event (no canvas_id match); dispatch runs
    # and logs without erroring.
    await gardener.on_canvas_event(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": "c1", "page_path": "x.md"},
    )
    await gardener.wait_idle()
    # Just verifying no exception escaped the non-blocking path.


async def test_pattern_cache_invalidates_on_workflow_canvas_event(gardener_env):
    gardener, _, _, _ = gardener_env
    # Pre-seed cache + workflow canvas id.
    gardener.patterns.set_workflow_canvas_id("canvas_wp")
    gardener.patterns.put("some-pattern", "body", {})
    gardener.patterns.mark_loaded()
    # Event on the workflow canvas flushes the cache.
    await gardener.on_canvas_event(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": "canvas_wp", "page_path": "01-software-development.md"},
    )
    await gardener.wait_idle()
    assert gardener.patterns.loaded is False
