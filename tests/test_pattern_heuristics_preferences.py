"""CANVAS-GARDENER-PREFERENCE-CAPTURE Commit 4 — preference-aware dispatch.

Covers:
  - HeuristicDecl parses suppressed_by_preference + threshold_preference fields
  - evaluate_declaration honors suppression gate (truthy pref → no fire)
  - evaluate_declaration honors threshold override (pref value replaces declared)
  - Backward compatibility: declarations without either field behave exactly as
    before (no preferences passed in; regression unchanged)
  - Gardener dispatch populates eval_state.preferences from canvas.yaml
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.gardener import GardenerService
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.pattern_heuristics import (
    CanvasEvaluationState,
    HeuristicDecl,
    _decl_with_threshold_override,
    evaluate_declaration,
    parse_heuristic_declarations,
)


INSTANCE = "inst_prefdispatch"
OPERATOR = "member:inst_prefdispatch:owner"


# ---- Parser round-trip ---------------------------------------------------


def test_parser_accepts_suppressed_by_preference_field():
    body = (
        "## Evolution heuristics\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: h-suppressed\n"
        "    trigger: page-created\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: page-count\n"
        "      params: {path_glob: 'x/*.md', threshold: 3}\n"
        "    action: {kind: propose_split}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "    suppressed_by_preference: rsvp-routing\n"
        "```\n"
    )
    decls = parse_heuristic_declarations(body)
    assert len(decls) == 1
    assert decls[0].suppressed_by_preference == "rsvp-routing"
    assert decls[0].threshold_preference == ""


def test_parser_accepts_threshold_preference_field():
    body = (
        "## Evolution heuristics\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: h-threshold\n"
        "    trigger: page-changed\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: duration-since-write\n"
        "      params: {threshold_days: 90}\n"
        "    action: {kind: flag}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "    threshold_preference: staleness-days\n"
        "```\n"
    )
    decls = parse_heuristic_declarations(body)
    assert decls[0].threshold_preference == "staleness-days"


def test_parser_declaration_without_fields_back_compat():
    """Existing declarations without the new fields parse unchanged."""
    body = (
        "## Evolution heuristics\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: h-plain\n"
        "    trigger: page-created\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: page-count\n"
        "      params: {path_glob: 'x/*.md', threshold: 3}\n"
        "    action: {kind: propose_split}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "```\n"
    )
    decls = parse_heuristic_declarations(body)
    assert decls[0].suppressed_by_preference == ""
    assert decls[0].threshold_preference == ""


# ---- Suppression gate ----------------------------------------------------


def _base_decl() -> HeuristicDecl:
    return HeuristicDecl(
        id="t", trigger="page-created",
        signal={"type": "deterministic", "check": "page-count",
                "params": {"path_glob": "specs/*.md", "threshold": 3}},
        action={"kind": "propose_subdivide"},
        confidence="deterministic-high", status="active",
    )


def _state_with_specs(count: int, preferences: dict | None = None) -> CanvasEvaluationState:
    return CanvasEvaluationState(
        canvas_id="c1",
        page_index=[{"path": f"specs/s-{i}.md"} for i in range(count)],
        page_path="specs/s-0.md",
        page_body="body",
        preferences=dict(preferences or {}),
    )


def test_suppression_skips_evaluation_when_pref_truthy():
    decl = replace(_base_decl(), suppressed_by_preference="quiet-specs")
    # Absent preference → evaluation runs (5 specs > threshold 3 → fires).
    state = _state_with_specs(5)
    assert evaluate_declaration(decl, state) is not None
    # Truthy preference → suppresses.
    state = _state_with_specs(5, preferences={"quiet-specs": True})
    assert evaluate_declaration(decl, state) is None


def test_suppression_falsy_preference_does_not_suppress():
    """Falsy values (False, 0, empty string) don't count as suppression."""
    decl = replace(_base_decl(), suppressed_by_preference="quiet-specs")
    state = _state_with_specs(5, preferences={"quiet-specs": False})
    # 5 specs still trips the threshold.
    assert evaluate_declaration(decl, state) is not None
    state = _state_with_specs(5, preferences={"quiet-specs": ""})
    assert evaluate_declaration(decl, state) is not None
    state = _state_with_specs(5, preferences={"quiet-specs": 0})
    assert evaluate_declaration(decl, state) is not None


def test_suppression_truthy_string_counts():
    decl = replace(_base_decl(), suppressed_by_preference="routing-mode")
    state = _state_with_specs(5, preferences={"routing-mode": "silent"})
    assert evaluate_declaration(decl, state) is None


def test_no_preferences_in_state_equals_no_suppression():
    """Empty preferences dict behaves identically to no suppression field set."""
    decl = replace(_base_decl(), suppressed_by_preference="does-not-exist")
    state = _state_with_specs(5)
    # Preference not set → heuristic fires normally.
    assert evaluate_declaration(decl, state) is not None


# ---- Threshold override -------------------------------------------------


def test_threshold_preference_overrides_page_count_threshold():
    decl = replace(_base_decl(), threshold_preference="spec-cap")
    # Baseline: 5 specs with default threshold 3 → fires.
    state = _state_with_specs(5)
    assert evaluate_declaration(decl, state) is not None
    # Override to 10: 5 specs now below threshold → no fire.
    state = _state_with_specs(5, preferences={"spec-cap": 10})
    assert evaluate_declaration(decl, state) is None
    # Override to 2: fires even more easily.
    state = _state_with_specs(5, preferences={"spec-cap": 2})
    assert evaluate_declaration(decl, state) is not None


def test_threshold_preference_overrides_duration_threshold():
    """duration-since-write uses threshold_days (different key than threshold)."""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    decl = HeuristicDecl(
        id="d", trigger="page-changed",
        signal={"type": "deterministic", "check": "duration-since-write",
                "params": {"page_path": "phase.md", "threshold_days": 90}},
        action={"kind": "flag"},
        confidence="deterministic-high", status="active",
        threshold_preference="staleness-days",
    )
    state = CanvasEvaluationState(
        canvas_id="c1",
        page_index=[{"path": "phase.md", "last_updated": old}],
        page_path="other.md",
        preferences={},
    )
    # Default threshold 90 days — page only 30 days old → no fire.
    assert evaluate_declaration(decl, state) is None
    # Override to 7 days — now fires.
    state = CanvasEvaluationState(
        canvas_id="c1",
        page_index=[{"path": "phase.md", "last_updated": old}],
        page_path="other.md",
        preferences={"staleness-days": 7},
    )
    assert evaluate_declaration(decl, state) is not None


def test_threshold_preference_noop_when_pref_missing():
    """Threshold override is only applied when the preference is actually set."""
    decl = replace(_base_decl(), threshold_preference="spec-cap")
    # pref absent → default threshold applies → 5 > 3 fires.
    state = _state_with_specs(5)
    assert evaluate_declaration(decl, state) is not None
    # pref explicitly None → no override (None treated as "unset").
    state = _state_with_specs(5, preferences={"spec-cap": None})
    assert evaluate_declaration(decl, state) is not None


def test_both_suppression_and_threshold_declared():
    """Suppression gates first — if suppressed, threshold override is moot."""
    decl = replace(
        _base_decl(),
        suppressed_by_preference="route-mode",
        threshold_preference="spec-cap",
    )
    # Suppressed: skip regardless of threshold override.
    state = _state_with_specs(100, preferences={"route-mode": "silent",
                                                  "spec-cap": 1})
    assert evaluate_declaration(decl, state) is None
    # Not suppressed + threshold override in effect.
    state = _state_with_specs(5, preferences={"spec-cap": 10})
    assert evaluate_declaration(decl, state) is None   # 5 < 10, no fire
    state = _state_with_specs(5, preferences={"spec-cap": 2})
    assert evaluate_declaration(decl, state) is not None   # 5 > 2, fires


def test_decl_with_threshold_override_is_side_effect_free():
    """Override helper returns a new decl — doesn't mutate the cached one."""
    original = replace(_base_decl(), threshold_preference="spec-cap")
    assert original.signal["params"]["threshold"] == 3
    replaced = _decl_with_threshold_override(original, 99)
    assert replaced.signal["params"]["threshold"] == 99
    # Original unchanged.
    assert original.signal["params"]["threshold"] == 3


def test_decl_with_threshold_override_falls_through_when_no_threshold_key():
    """Returns original decl unchanged when signal.params has no threshold-like key."""
    decl = HeuristicDecl(
        id="x", trigger="page-created",
        signal={"type": "deterministic", "check": "missing-frontmatter-field",
                "params": {"field": "pillar"}},
        action={"kind": "flag"}, confidence="deterministic-high",
        status="active",
    )
    replaced = _decl_with_threshold_override(decl, "whatever")
    assert replaced is decl


# ---- Gardener dispatch populates eval_state.preferences -----------------


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))

    class _Stub:
        async def complete_simple(self, *a, **kw):
            raise RuntimeError("no LLM in this test")
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=_Stub(),
    )
    yield gardener, svc, idb
    await gardener.wait_idle()
    await idb.close()


async def test_dispatch_reads_preferences_from_canvas_yaml(env):
    """Gardener dispatch pulls confirmed preferences from canvas.yaml into
    eval_state.preferences. We verify via a pattern that declares
    suppressed_by_preference: the heuristic fires without the pref set,
    doesn't fire with it set."""
    gardener, svc, _ = env
    # Seed a pattern body with a declared-suppressible heuristic.
    pattern_body = (
        "---\n"
        "scope: team\ntype: note\npattern: test-pattern\nconsumer: gardener\n"
        "---\n"
        "\n## Evolution heuristics\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: suppressed-test\n"
        "    trigger: page-created\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: page-count\n"
        "      params: {path_glob: 'specs/*.md', threshold: 2}\n"
        "    action: {kind: propose_subdivide}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "    suppressed_by_preference: quiet\n"
        "```\n\n## End\n"
    )
    gardener.patterns.put("test-pattern", pattern_body, {"pattern": "test-pattern"})
    gardener.patterns.mark_loaded()

    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.set_canvas_pattern(
        instance_id=INSTANCE, canvas_id=c.canvas_id, pattern="test-pattern",
    )
    # Write 3 specs — above threshold 2.
    for i in range(3):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=c.canvas_id,
            page_slug=f"specs/s-{i}.md", body="body",
            writer_member_id=OPERATOR,
        )

    # No preference set → heuristic fires.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "specs/s-2.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    assert any(p.action == "propose_subdivide" for p in buffered)

    # Now set the suppressing preference and clear the coalescer.
    gardener.coalescer._buffers.pop(c.canvas_id, None)
    await svc.set_preference(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        name="quiet", value=True,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "specs/s-2.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    assert not any(p.action == "propose_subdivide" for p in buffered)
