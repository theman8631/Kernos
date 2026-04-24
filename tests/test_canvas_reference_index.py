"""CANVAS-CROSS-PAGE-INDEX — reference-index parser, storage, dispatch.

Scope:
  - Pure parser: parse_wiki_links, wiki_link_locations, slug normalization
  - ReferenceIndex dataclass: outbound maintenance, inbound queries,
    broken-reference detection
  - Persistence: load / save / cold-start rebuild from canvas pages
  - CanvasService integration: page_write updates the index inline
  - Gardener dispatch: back-reference-promotion heuristic + broken-ref
    finding surface through Batch B's coalescer/consent infrastructure
  - Regression: existing Pattern 00 heuristics + pattern dispatch unchanged
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.canvas import CanvasService, canvas_dir
from kernos.kernel.canvas_reference_index import (
    BrokenReference,
    ReferenceIndex,
    index_path,
    load_or_empty,
    parse_wiki_links,
    path_from_slug,
    rebuild_from_canvas,
    save as save_index,
    slug_from_path,
    wiki_link_locations,
)
from kernos.kernel.gardener import (
    BACK_REFERENCE_PROMOTION_THRESHOLD,
    GardenerService,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_reftest"
OPERATOR = "member:inst_reftest:owner"


# ---- Parser + slug helpers ------------------------------------------------


def test_parse_wiki_links_empty_body():
    assert parse_wiki_links("") == []
    assert parse_wiki_links("plain text no links") == []


def test_parse_wiki_links_basic():
    assert parse_wiki_links("Mentions [[charter]] and [[architecture]].") == [
        "charter", "architecture",
    ]


def test_parse_wiki_links_preserves_duplicates():
    body = "See [[charter]] and [[charter]] again, plus [[architecture]]."
    assert parse_wiki_links(body) == ["charter", "charter", "architecture"]


def test_parse_wiki_links_nested_paths():
    body = "[[specs/launch]] and [[specs/checkout/flow]] are both nested."
    assert parse_wiki_links(body) == ["specs/launch", "specs/checkout/flow"]


def test_parse_wiki_links_strips_md_extension():
    assert parse_wiki_links("[[charter.md]] normalizes.") == ["charter"]


def test_parse_wiki_links_ignores_single_bracket():
    assert parse_wiki_links("Not a link [single] and [also single].") == []


def test_parse_wiki_links_ignores_empty_brackets():
    assert parse_wiki_links("[[]] should be skipped.") == []


def test_wiki_link_locations_tracks_every_occurrence():
    body = "First [[charter]] then [[charter.md]] later."
    locations = wiki_link_locations(body, "charter")
    # Two occurrences, both matching after normalization.
    assert len(locations) == 2
    assert locations[0] < locations[1]


def test_slug_from_path_normalizes_extension():
    assert slug_from_path("specs/launch.md") == "specs/launch"
    assert slug_from_path("index.md") == "index"
    assert slug_from_path("specs/launch") == "specs/launch"


def test_path_from_slug_adds_md_extension():
    assert path_from_slug("charter") == "charter.md"
    assert path_from_slug("specs/launch") == "specs/launch.md"
    # Idempotent when already has extension.
    assert path_from_slug("charter.md") == "charter.md"


# ---- ReferenceIndex unit tests --------------------------------------------


def test_update_page_stores_targets():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("specs/a.md", ["charter", "architecture"])
    assert idx.outbound["specs/a"] == ["charter", "architecture"]


def test_update_page_with_empty_targets_removes_entry():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("specs/a.md", ["charter"])
    idx.update_page("specs/a.md", [])  # removed all links
    assert "specs/a" not in idx.outbound


def test_count_inbound_sums_across_sources_with_multiplicity():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("specs/a.md", ["charter"])
    idx.update_page("specs/b.md", ["charter"])
    idx.update_page("specs/c.md", ["charter", "charter"])  # duplicate
    assert idx.count_inbound("charter") == 4


def test_count_inbound_by_source_excludes_zero_refs():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("specs/a.md", ["charter"])
    idx.update_page("specs/b.md", ["architecture"])
    by_source = idx.count_inbound_by_source("charter")
    assert by_source == {"specs/a": 1}


def test_all_inbound_counts():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("a", ["x", "y"])
    idx.update_page("b", ["x", "z"])
    assert idx.all_inbound_counts() == {"x": 2, "y": 1, "z": 1}


def test_forget_page_removes_outbound_entry():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("a", ["x"])
    idx.forget_page("a")
    assert "a" not in idx.outbound


def test_find_broken_references_returns_targets_outside_canvas():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("a", ["x", "missing"])
    idx.update_page("b", ["missing", "missing"])
    known = {"a", "b", "x"}
    broken = idx.find_broken_references(known)
    # Two broken refs: a→missing (count 1), b→missing (count 2)
    by_source = {br.source_slug: br for br in broken}
    assert by_source["a"].target_slug == "missing"
    assert by_source["a"].count == 1
    assert by_source["b"].target_slug == "missing"
    assert by_source["b"].count == 2


def test_find_broken_references_empty_when_all_targets_exist():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("a", ["x", "y"])
    assert idx.find_broken_references({"a", "x", "y"}) == []


# ---- Persistence ---------------------------------------------------------


def test_to_json_and_from_json_roundtrip():
    idx = ReferenceIndex(canvas_id="c1")
    idx.update_page("a", ["x", "y"])
    idx.update_page("b", ["x"])
    j = idx.to_json()
    restored = ReferenceIndex.from_json(j)
    assert restored.canvas_id == "c1"
    assert restored.outbound == {"a": ["x", "y"], "b": ["x"]}


def test_load_or_empty_missing_file(tmp_path):
    idx = load_or_empty(str(tmp_path), "inst", "canvas_missing")
    assert idx.canvas_id == "canvas_missing"
    assert idx.outbound == {}


def test_save_and_reload_roundtrip(tmp_path):
    idx = ReferenceIndex(canvas_id="canvas_x")
    idx.update_page("a", ["x"])
    save_index(idx, str(tmp_path), "inst")
    # File exists at expected location.
    expected_path = index_path(str(tmp_path), "inst", "canvas_x")
    assert expected_path.is_file()
    reloaded = load_or_empty(str(tmp_path), "inst", "canvas_x")
    assert reloaded.outbound == {"a": ["x"]}


def test_load_or_empty_malformed_json_returns_empty(tmp_path):
    path = index_path(str(tmp_path), "inst", "canvas_bad")
    path.parent.mkdir(parents=True)
    path.write_text("{not-json:", encoding="utf-8")
    idx = load_or_empty(str(tmp_path), "inst", "canvas_bad")
    assert idx.outbound == {}


# ---- CanvasService integration -------------------------------------------


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    yield svc, idb, tmp_path
    await idb.close()


async def test_index_updated_before_event_emission(env):
    """Load-bearing ordering invariant: the reference index reflects the new
    outbound edge BEFORE canvas.page.* events fire — the Gardener's
    ``asyncio.create_task`` dispatch queries the index, so events firing on
    stale state would race.

    Mechanism: replace ``canvas_service._emit`` with a wrapper that, at each
    emission, reads the current in-memory index state and captures it. After
    a page_write completes, every captured canvas.page.* emission should
    show the new outbound edge already present.
    """
    svc, idb, tmp_path = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    original_emit = svc._emit
    captured: list[dict] = []

    async def capturing_emit(instance_id, event_type, payload, *, member_id=""):
        # Snapshot index state at the exact moment the event fires.
        idx = await svc.get_reference_index(
            instance_id=instance_id, canvas_id=c.canvas_id,
        )
        captured.append({
            "event_type": event_type,
            "outbound_at_emission": dict(idx.outbound),
        })
        await original_emit(instance_id, event_type, payload, member_id=member_id)

    svc._emit = capturing_emit

    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="page-a.md",
        body="Refers to [[charter]] and [[architecture]].",
        writer_member_id=OPERATOR,
    )

    page_events = [e for e in captured if e["event_type"].startswith("canvas.page.")]
    assert page_events, "at least one canvas.page.* event should have emitted"
    for e in page_events:
        outbound = e["outbound_at_emission"]
        assert outbound.get("page-a") == ["charter", "architecture"], (
            f"{e['event_type']} fired before reference index was updated — "
            f"ordering invariant violated. Captured outbound state: {outbound}"
        )


async def test_page_write_updates_index(env):
    svc, idb, tmp_path = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    # Two targets, one referenced twice.
    body = "Body [[charter]] and [[charter]] and [[architecture]]."
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="specs/a.md", body=body, writer_member_id=OPERATOR,
    )
    idx = await svc.get_reference_index(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
    )
    assert idx.outbound["specs/a"] == ["charter", "charter", "architecture"]


async def test_page_write_persists_index_to_disk(env):
    svc, idb, tmp_path = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="specs/a.md", body="See [[charter]].",
        writer_member_id=OPERATOR,
    )
    # File should exist; content should reflect our write.
    path = index_path(str(tmp_path), INSTANCE, c.canvas_id)
    assert path.is_file()
    data = json.loads(path.read_text())
    assert "specs/a" in data["outbound"]


async def test_page_write_broken_references_surfaced_in_extra(env):
    svc, idb, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    # Write a page that links to a page that doesn't exist.
    result = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="specs/a.md",
        body="I reference [[nonexistent]] and [[also-missing]].",
        writer_member_id=OPERATOR,
    )
    broken = result.extra.get("broken_references", [])
    targets = sorted(b["target"] for b in broken)
    assert "nonexistent" in targets
    assert "also-missing" in targets


async def test_cold_start_rebuild_populates_from_pages(env):
    svc, idb, tmp_path = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="a.md", body="[[charter]]",
        writer_member_id=OPERATOR,
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="b.md", body="[[charter]] [[architecture]]",
        writer_member_id=OPERATOR,
    )
    # Delete the index file + clear the in-memory cache → next access
    # triggers cold-start rebuild.
    index_path(str(tmp_path), INSTANCE, c.canvas_id).unlink()
    svc._ref_indexes.clear()
    idx = await svc.get_reference_index(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
    )
    assert idx.outbound.get("a") == ["charter"]
    assert idx.outbound.get("b") == ["charter", "architecture"]


async def test_rebuild_from_canvas_directly(env):
    """Explicit rebuild helper for operator-triggered recovery."""
    svc, idb, tmp_path = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="page-a.md", body="[[charter]]",
        writer_member_id=OPERATOR,
    )
    idx = await rebuild_from_canvas(
        canvas_service=svc, instance_id=INSTANCE, canvas_id=c.canvas_id,
    )
    assert idx.outbound.get("page-a") == ["charter"]


async def test_no_auto_rewrite_on_broken_reference(env):
    """Spec invariant: broken refs surface, they don't rewrite the body."""
    svc, idb, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    body = "[[nonexistent-target]]"
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="a.md", body=body, writer_member_id=OPERATOR,
    )
    # Body on disk should still contain the broken reference verbatim.
    read = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="a.md",
    )
    assert "[[nonexistent-target]]" in read.extra["body"]


# ---- Gardener dispatch integration ---------------------------------------


class _NullReasoning:
    async def complete_simple(self, *a, **kw):
        raise RuntimeError("ref-index tests must not hit the LLM path")


@pytest.fixture
async def dispatch_env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb,
        reasoning_service=_NullReasoning(),
    )
    yield gardener, svc, idb, tmp_path
    await gardener.wait_idle()
    await idb.close()


async def test_back_reference_promotion_fires_at_threshold(dispatch_env):
    gardener, svc, idb, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    # Seed the target page first.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="hub.md", body="I am referenced by many.",
        writer_member_id=OPERATOR,
    )
    # Create BACK_REFERENCE_PROMOTION_THRESHOLD sources linking to it.
    for i in range(BACK_REFERENCE_PROMOTION_THRESHOLD):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=c.canvas_id,
            page_slug=f"source-{i}.md", body=f"Refers to [[hub]].",
            writer_member_id=OPERATOR,
        )
    # Dispatch a canvas.page.changed on hub itself so the heuristic checks
    # hub's inbound count.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="hub.md", body="Updated body.",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": c.canvas_id, "page_path": "hub.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    assert any(p.action == "propose_back_reference_index" for p in buffered)


async def test_back_reference_promotion_quiet_under_threshold(dispatch_env):
    gardener, svc, _, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="hub.md", body="x", writer_member_id=OPERATOR,
    )
    # Only 2 inbound — under the threshold of 3.
    for i in range(BACK_REFERENCE_PROMOTION_THRESHOLD - 1):
        await svc.page_write(
            instance_id=INSTANCE, canvas_id=c.canvas_id,
            page_slug=f"s-{i}.md", body="[[hub]]", writer_member_id=OPERATOR,
        )
    await gardener._dispatch(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": c.canvas_id, "page_path": "hub.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    assert not any(p.action == "propose_back_reference_index" for p in buffered)


async def test_broken_reference_finding_surfaces(dispatch_env):
    gardener, svc, _, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="orphan.md",
        body="Link to [[missing-page]] that doesn't exist.",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "orphan.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    broken = [p for p in buffered if p.action == "flag_broken_reference"]
    assert broken
    # Repair-grade provenance: source + target + count.
    p = broken[0]
    assert p.payload["source_page"] == "orphan"
    assert p.payload["target_slug"] == "missing-page"
    assert p.payload["count"] >= 1
    assert p.payload["reason"] == "target_missing"


async def test_broken_reference_count_reflects_multiplicity(dispatch_env):
    gardener, svc, _, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="multi.md",
        body="[[missing]] and [[missing]] and [[missing]].",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "multi.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    broken = [p for p in buffered if p.action == "flag_broken_reference"]
    assert broken
    assert broken[0].payload["count"] == 3


async def test_no_broken_ref_when_target_exists(dispatch_env):
    gardener, svc, _, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="target.md", body="I exist.",
        writer_member_id=OPERATOR,
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="source.md", body="[[target]]",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "source.md"},
    )
    buffered = gardener.coalescer._buffers.get(c.canvas_id, [])
    assert not any(p.action == "flag_broken_reference" for p in buffered)


async def test_dispatch_does_not_hit_llm_path(dispatch_env):
    """Back-reference-promotion + broken-ref findings are deterministic."""
    gardener, svc, _, _ = dispatch_env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="a.md", body="[[missing]]",
        writer_member_id=OPERATOR,
    )
    # _NullReasoning raises on complete_simple; dispatch must not invoke it.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "a.md"},
    )
    # If we reached here, no LLM was called.


async def test_pattern_02_npc_promote_on_mention_stays_disabled():
    """Regression guard: the three reference-dependent Batch C declarations
    stay status=disabled in this batch (spec acceptance criterion 9)."""
    from kernos.kernel.pattern_heuristics import parse_heuristic_declarations
    body = (
        Path("docs/workflow-patterns/02-long-form-campaign.md")
        .read_text(encoding="utf-8")
    )
    decls = parse_heuristic_declarations(body)
    ids = {d.id: d for d in decls}
    assert ids["npc-promote-on-mention"].status == "disabled"


async def test_pattern_01_reference_dependent_declarations_stay_disabled():
    """Pattern 01's deferred-promotion + component-decision-pressure stay disabled."""
    from kernos.kernel.pattern_heuristics import parse_heuristic_declarations
    body = (
        Path("docs/workflow-patterns/01-software-development.md")
        .read_text(encoding="utf-8")
    )
    decls = parse_heuristic_declarations(body)
    ids = {d.id: d for d in decls}
    assert ids["deferred-promotion"].status == "disabled"
    assert ids["component-decision-pressure"].status == "disabled"
