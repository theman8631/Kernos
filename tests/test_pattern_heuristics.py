"""CANVAS-GARDENER-PATTERN-HEURISTICS — format + dispatch coverage.

Scope:
  - Parser + deterministic check handlers (pure, no async)
  - GardenerService dispatch: Pattern 01 active heuristics fire against a
    software-development canvas; no Pattern 01 fires against other patterns
  - Upgrade-finding path: canvas whose pattern page lacks declarations
    surfaces flag_library_upgrade instead of silent Pattern-00-only
  - Repo-inventory: Pattern 01 + Pattern 02 library files parse cleanly,
    prose sentences are preserved byte-identical in the evolution section

Semantic heuristics ship status=disabled by default per spec; no live
LLM path is exercised in this batch (see batch report).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.canvas import CanvasService
from kernos.kernel.gardener import (
    GardenerService,
    WORKFLOW_PATTERNS_CANVAS_NAME,
    _CachedPattern,
)
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.pattern_heuristics import (
    CanvasEvaluationState,
    HeuristicDecl,
    VALID_ACTION_KINDS,
    VALID_CONFIDENCE,
    VALID_DETERMINISTIC_CHECKS,
    VALID_STATUS,
    VALID_TRIGGERS,
    evaluate_declaration,
    extract_declarations_block,
    parse_heuristic_declarations,
    trigger_matches_event,
)


LIBRARY_DIR = Path(__file__).resolve().parents[1] / "docs" / "workflow-patterns"
INSTANCE = "inst_patternheur"
OPERATOR = "member:inst_patternheur:owner"


# ---- Parser ---------------------------------------------------------------


def test_extract_block_missing_section_returns_empty():
    assert extract_declarations_block("") == ""
    assert extract_declarations_block("# Page\n\nNo heuristics section here.\n") == ""


def test_extract_block_section_but_no_fence_returns_empty():
    body = "## Evolution heuristics\n\nProse only, no fenced YAML.\n\n## Next\n"
    assert extract_declarations_block(body) == ""


def test_parse_minimal_active_declaration():
    body = (
        "## Evolution heuristics\n\n"
        "Prose here.\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: sample\n"
        "    trigger: page-created\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: page-count\n"
        "      params: {path_glob: 'specs/*.md', threshold: 5}\n"
        "    action:\n"
        "      kind: propose_subdivide\n"
        "    confidence: deterministic-high\n"
        "    coalesce: {key: sample}\n"
        "    status: active\n"
        "```\n\n## Next section\n"
    )
    decls = parse_heuristic_declarations(body)
    assert len(decls) == 1
    d = decls[0]
    assert d.id == "sample"
    assert d.is_active
    assert not d.is_semantic
    assert d.action["kind"] == "propose_subdivide"


def test_parse_skips_invalid_declarations_without_dropping_valid_ones():
    body = (
        "## Evolution heuristics\n\n"
        "```yaml\n"
        "heuristics:\n"
        "  - id: invalid-1\n"
        "    trigger: not-a-real-trigger\n"
        "    signal: {type: deterministic, check: page-count}\n"
        "    action: {kind: propose_split}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "  - id: valid\n"
        "    trigger: page-created\n"
        "    signal:\n"
        "      type: deterministic\n"
        "      check: page-count\n"
        "      params: {path_glob: 'x/*.md', threshold: 1}\n"
        "    action: {kind: propose_subdivide}\n"
        "    confidence: deterministic-high\n"
        "    status: active\n"
        "```\n"
    )
    decls = parse_heuristic_declarations(body)
    assert [d.id for d in decls] == ["valid"]


def test_parse_malformed_yaml_is_swallowed():
    body = (
        "## Evolution heuristics\n\n```yaml\nheuristics: [this is not valid YAML: : :\n```\n"
    )
    assert parse_heuristic_declarations(body) == []


def test_vocabulary_enums_populated():
    # Sanity — the declared vocabularies aren't empty after module edits.
    assert VALID_TRIGGERS
    assert VALID_DETERMINISTIC_CHECKS
    assert VALID_ACTION_KINDS
    assert "deterministic-high" in VALID_CONFIDENCE
    assert "active" in VALID_STATUS and "disabled" in VALID_STATUS


# ---- trigger_matches_event -----------------------------------------------


def _decl(trigger: str, scope: dict | None = None) -> HeuristicDecl:
    return HeuristicDecl(
        id="t", trigger=trigger,
        signal={"type": "deterministic", "check": "page-count",
                "params": {"path_glob": "x", "threshold": 1}},
        action={"kind": "flag"},
        confidence="deterministic-high",
        status="active",
        scope=scope or {},
    )


def test_trigger_page_created_matches_canvas_page_created():
    assert trigger_matches_event(_decl("page-created"), "canvas.page.created", {})
    assert not trigger_matches_event(_decl("page-created"), "canvas.page.changed", {})


def test_trigger_page_changed_matches_both_changed_and_created():
    d = _decl("page-changed")
    assert trigger_matches_event(d, "canvas.page.changed", {})
    assert trigger_matches_event(d, "canvas.page.created", {})


def test_trigger_page_state_changed_respects_to_state_filter():
    d_any = _decl("page-state-changed")
    d_approved = _decl("page-state-changed", scope={"to_state": "approved"})
    payload = {"new_state": "approved"}
    assert trigger_matches_event(d_any, "canvas.page.state_changed", payload)
    assert trigger_matches_event(d_approved, "canvas.page.state_changed", payload)
    # Different target state: scoped decl doesn't match.
    assert not trigger_matches_event(
        d_approved, "canvas.page.state_changed", {"new_state": "drafted"},
    )


def test_trigger_page_reference_added_never_matches_in_v1():
    """Blocked on CANVAS-CROSS-PAGE-INDEX; never matches anything yet."""
    d = _decl("page-reference-added")
    assert not trigger_matches_event(d, "canvas.page.created", {})
    assert not trigger_matches_event(d, "canvas.page.changed", {})


def test_periodic_triggers_never_match_page_events():
    for trig in ("periodic-daily", "periodic-weekly", "periodic-monthly"):
        assert not trigger_matches_event(_decl(trig), "canvas.page.created", {})


# ---- Deterministic check handlers ----------------------------------------


def _eval_state(**overrides) -> CanvasEvaluationState:
    base = {
        "canvas_id": "c1",
        "page_index": [],
        "page_path": "",
        "page_body": "",
        "page_frontmatter": {},
        "event_type": "canvas.page.created",
        "event_payload": {},
        "canvas_anchors": {},
    }
    base.update(overrides)
    return CanvasEvaluationState(**base)


def test_page_count_fires_above_threshold():
    decl = HeuristicDecl(
        id="page-count-test", trigger="page-created",
        signal={"type": "deterministic", "check": "page-count",
                "params": {"path_glob": "specs/*.md", "threshold": 2}},
        action={"kind": "propose_subdivide"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(page_index=[
        {"path": "specs/a.md"}, {"path": "specs/b.md"}, {"path": "specs/c.md"},
        {"path": "other.md"},
    ])
    match = evaluate_declaration(decl, state)
    assert match is not None
    assert match.fired
    assert match.payload["count"] == 3


def test_page_count_quiet_when_at_threshold():
    decl = HeuristicDecl(
        id="x", trigger="page-created",
        signal={"type": "deterministic", "check": "page-count",
                "params": {"path_glob": "s/*.md", "threshold": 3}},
        action={"kind": "propose_subdivide"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(page_index=[{"path": f"s/{n}.md"} for n in "abc"])
    assert evaluate_declaration(decl, state) is None


def test_page_size_lines_fires():
    decl = HeuristicDecl(
        id="size", trigger="page-changed",
        signal={"type": "deterministic", "check": "page-size-lines",
                "params": {"threshold": 10}},
        action={"kind": "propose_split"},
        confidence="deterministic-high", status="active",
    )
    big = "\n".join(["line"] * 20)
    state = _eval_state(page_body=big, page_path="specs/a.md")
    match = evaluate_declaration(decl, state)
    assert match and match.fired


def test_duration_since_write_uses_target_page():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    decl = HeuristicDecl(
        id="dur", trigger="page-changed",
        signal={"type": "deterministic", "check": "duration-since-write",
                "params": {"page_path": "phase.md", "threshold_days": 14}},
        action={"kind": "propose_transition"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(
        page_index=[{"path": "phase.md", "last_updated": old}],
        page_path="other.md",
    )
    match = evaluate_declaration(decl, state)
    assert match and match.payload["age_days"] >= 14


def test_missing_frontmatter_field_fires_when_absent():
    decl = HeuristicDecl(
        id="fm", trigger="page-state-changed",
        signal={"type": "deterministic", "check": "missing-frontmatter-field",
                "params": {"field": "pillar"}},
        action={"kind": "flag"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(page_frontmatter={"title": "x"}, page_path="specs/a.md")
    match = evaluate_declaration(decl, state)
    assert match and match.fired


def test_missing_frontmatter_field_quiet_when_present():
    decl = HeuristicDecl(
        id="fm", trigger="page-state-changed",
        signal={"type": "deterministic", "check": "missing-frontmatter-field",
                "params": {"field": "pillar"}},
        action={"kind": "flag"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(page_frontmatter={"pillar": "pillar-a"}, page_path="specs/a.md")
    assert evaluate_declaration(decl, state) is None


def test_date_relative_window_fires_inside_window():
    future = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
    decl = HeuristicDecl(
        id="d", trigger="date-relative",
        signal={"type": "deterministic", "check": "date-relative-window",
                "params": {"anchor": "event_date", "offset_days": -90}},
        action={"kind": "propose_transition"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(canvas_anchors={"event_date": future})
    match = evaluate_declaration(decl, state)
    assert match and match.fired


def test_date_relative_window_quiet_outside_window():
    future = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
    decl = HeuristicDecl(
        id="d", trigger="date-relative",
        signal={"type": "deterministic", "check": "date-relative-window",
                "params": {"anchor": "event_date", "offset_days": -90}},
        action={"kind": "propose_transition"},
        confidence="deterministic-high", status="active",
    )
    state = _eval_state(canvas_anchors={"event_date": future})
    assert evaluate_declaration(decl, state) is None


def test_reference_count_returns_none_pending_index():
    """Blocked on CANVAS-CROSS-PAGE-INDEX — handler exists for parser but no-ops."""
    decl = HeuristicDecl(
        id="r", trigger="page-changed",
        signal={"type": "deterministic", "check": "reference-count",
                "params": {"target": "x", "threshold": 3}},
        action={"kind": "propose_promote"},
        confidence="deterministic-high", status="active",
    )
    assert evaluate_declaration(decl, _eval_state()) is None


def test_disabled_declaration_does_not_evaluate():
    decl = HeuristicDecl(
        id="d", trigger="page-created",
        signal={"type": "deterministic", "check": "page-count",
                "params": {"path_glob": "x/*.md", "threshold": 1}},
        action={"kind": "propose_subdivide"},
        confidence="deterministic-high", status="disabled",
    )
    state = _eval_state(page_index=[{"path": "x/a.md"}, {"path": "x/b.md"}])
    assert evaluate_declaration(decl, state) is None


# ---- Repo inventory — shipped Pattern 01 + Pattern 02 --------------------


def test_pattern_01_declarations_parse_cleanly():
    body = (LIBRARY_DIR / "01-software-development.md").read_text(encoding="utf-8")
    decls = parse_heuristic_declarations(body)
    ids = {d.id for d in decls}
    # Shipped active heuristics.
    for active_id in (
        "spec-count-subdivision", "spec-size-split", "phase-focus-stale",
        "manifest-sync-lag", "spec-missing-pillar",
    ):
        assert active_id in ids, f"missing active decl: {active_id}"
    # Semantic heuristics shipped disabled by default.
    for semantic_id in ("pillar-conflict", "primitive-overlap"):
        assert semantic_id in ids
        decl = next(d for d in decls if d.id == semantic_id)
        assert decl.status == "disabled"
        assert decl.is_semantic


def test_pattern_02_smoke_migration_parses():
    body = (LIBRARY_DIR / "02-long-form-campaign.md").read_text(encoding="utf-8")
    decls = parse_heuristic_declarations(body)
    ids = {d.id for d in decls}
    assert "location-archive-stale" in ids
    assert "arc-stagnation" in ids
    # Semantic smoke example is disabled.
    assert "session-npc-heavy" in ids
    semantic = next(d for d in decls if d.id == "session-npc-heavy")
    assert semantic.status == "disabled"


def test_pattern_01_prose_sentences_preserved():
    """Acceptance criterion 7 — prose byte-identical. We spot-check the
    signature sentences of each heuristic section still appear verbatim."""
    body = (LIBRARY_DIR / "01-software-development.md").read_text(encoding="utf-8")
    for sentence in (
        "Spec count in `specs/` exceeds 12 without subdivision",
        "Single spec grows past ~400 lines",
        "Phase Map `current focus` unchanged for 3+ weeks",
        "Same component referenced in 5+ decisions",
        "Charter untouched 90+ days",
    ):
        assert sentence in body, f"prose drift: {sentence!r} missing"


def test_pattern_02_prose_sentences_preserved():
    body = (LIBRARY_DIR / "02-long-form-campaign.md").read_text(encoding="utf-8")
    for sentence in (
        "NPC mentioned in 3+ sessions",
        "Location appears in 5+ sessions",
        "Canon entry contradicts an earlier entry",
        "Faction appears in 5+ sessions",
    ):
        assert sentence in body, f"prose drift: {sentence!r} missing"


# ---- Gardener dispatch integration ---------------------------------------


class _NullReasoning:
    async def complete_simple(self, *a, **kw):
        raise RuntimeError("dispatch tests must not hit the LLM path")


@pytest.fixture
async def dispatch_env(tmp_path):
    """Instance + canvas service + a pre-loaded pattern cache."""
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb,
        reasoning_service=_NullReasoning(),
    )
    # Preload the pattern cache with the shipped Pattern 01 body so
    # dispatch uses it without re-reading the Workflow Patterns canvas
    # (which isn't seeded in this fixture).
    gardener.patterns.put(
        "software-development",
        (LIBRARY_DIR / "01-software-development.md").read_text(encoding="utf-8"),
        {"pattern": "software-development"},
    )
    gardener.patterns.put(
        "library-meta",
        (LIBRARY_DIR / "00-library-meta.md").read_text(encoding="utf-8"),
        {"pattern": "library-meta"},
    )
    gardener.patterns.set_workflow_canvas_id("canvas_preloaded")
    gardener.patterns.mark_loaded()
    yield gardener, svc, idb, tmp_path
    await gardener.wait_idle()
    await idb.close()


async def _make_software_canvas(
    svc: CanvasService, *, pattern: str = "software-development",
) -> str:
    """Create a canvas + stamp canvas.yaml with the pattern field."""
    create = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Test Project", scope="personal",
    )
    if pattern:
        await svc.set_canvas_pattern(
            instance_id=INSTANCE, canvas_id=create.canvas_id, pattern=pattern,
        )
    return create.canvas_id


async def test_dispatch_fires_pattern_01_spec_count_subdivision(dispatch_env):
    gardener, svc, idb, _ = dispatch_env
    canvas_id = await _make_software_canvas(svc)

    # Create 13 specs (exceeds threshold 12) so the heuristic matches on
    # the 13th page-created event.
    for i in range(13):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=canvas_id,
            page_slug=f"specs/spec-{i:02d}.md", body="body",
            writer_member_id=OPERATOR,
        )

    # Dispatch the canvas.page.created event for the 13th spec.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "specs/spec-12.md"},
    )
    buffered = [
        p for p in gardener.coalescer._buffers.get(canvas_id, [])
        if p.action == "propose_subdivide"
    ]
    assert buffered, "spec-count-subdivision should have coalesced a proposal"


async def test_dispatch_does_not_fire_pattern_01_on_non_matching_canvas(dispatch_env):
    gardener, svc, idb, _ = dispatch_env
    # Canvas with a different pattern — Pattern 01 must NOT fire.
    canvas_id = await _make_software_canvas(svc, pattern="creative-solo")

    for i in range(13):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=canvas_id,
            page_slug=f"specs/spec-{i:02d}.md", body="body",
            writer_member_id=OPERATOR,
        )
    # Prime the cache so the lookup would find a pattern if it tried.
    gardener.patterns.put(
        "creative-solo",
        "## Evolution heuristics\n\nProse only — no declarations.\n\n## Next\n",
        {"pattern": "creative-solo"},
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "specs/spec-12.md"},
    )
    proposals = gardener.coalescer._buffers.get(canvas_id, [])
    assert not any(p.action == "propose_subdivide" for p in proposals)
    # But the library-upgrade finding SHOULD fire since creative-solo's
    # library page has no declarations.
    assert any(p.action == "flag_library_upgrade" for p in proposals)


async def test_dispatch_upgrade_finding_on_pre_batch_pattern_page(dispatch_env):
    """Acceptance criterion 11: pre-batch library page → upgrade finding."""
    gardener, svc, idb, _ = dispatch_env
    canvas_id = await _make_software_canvas(svc, pattern="legal-case")

    # Preload a pattern body WITHOUT a fenced heuristics block.
    gardener.patterns.put(
        "legal-case",
        (
            "## Evolution heuristics\n\n"
            "Prose only — pre-batch library page, no declarations yet.\n\n"
            "## Next section\n"
        ),
        {"pattern": "legal-case"},
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=canvas_id,
        page_slug="something.md", body="body",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "something.md"},
    )
    buffered = gardener.coalescer._buffers.get(canvas_id, [])
    upgrade = [p for p in buffered if p.action == "flag_library_upgrade"]
    assert upgrade, "upgrade finding must surface instead of silent downgrade"
    assert "legal-case" in upgrade[0].rationale


async def test_dispatch_upgrade_finding_on_unknown_pattern(dispatch_env):
    """Canvas names a pattern the library doesn't carry → upgrade finding."""
    gardener, svc, idb, _ = dispatch_env
    canvas_id = await _make_software_canvas(svc, pattern="not-a-real-pattern")

    await svc.page_write(
        instance_id=INSTANCE, canvas_id=canvas_id,
        page_slug="p.md", body="body",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "p.md"},
    )
    buffered = gardener.coalescer._buffers.get(canvas_id, [])
    assert any(p.action == "flag_library_upgrade" for p in buffered)


async def test_pattern_heuristic_dispatch_does_not_call_llm(dispatch_env):
    """Acceptance criterion 4: deterministic heuristics evaluate without
    invoking the Gardener consultation LLM path."""
    gardener, svc, idb, _ = dispatch_env
    canvas_id = await _make_software_canvas(svc)

    # Create enough specs to fire spec-count-subdivision
    for i in range(13):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=canvas_id,
            page_slug=f"specs/s-{i}.md", body="b",
            writer_member_id=OPERATOR,
        )
    # _NullReasoning raises RuntimeError if complete_simple is called.
    # Dispatch must complete without raising.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "specs/s-12.md"},
    )
    # If we got here, no LLM call happened.
