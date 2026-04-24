"""CANVAS-SECTION-MARKERS Pillar 1 — section-aware reads/writes.

Spec: SPEC-CANVAS-SECTION-MARKERS + GARDENER COHORT, Pillar 1.
"""
from __future__ import annotations

import pytest

from kernos.kernel.canvas import (
    CanvasService,
    _slugify_heading,
    maybe_refresh_section_tokens,
    parse_sections,
    render_summary_view,
    replace_section_body,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_sectest"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    yield svc, idb, tmp_path
    await idb.close()


# ---- Pure helpers ---------------------------------------------------------


def test_slugify_basic():
    assert _slugify_heading("First Section") == "first-section"


def test_slugify_strips_punctuation():
    assert _slugify_heading("First Section: A Hero's Arc") == "first-section-a-heros-arc"


def test_parse_sections_empty_body():
    preamble, sections = parse_sections("")
    assert preamble == ""
    assert sections == []


def test_parse_sections_no_headings_is_backcompat():
    """Body without H2 headings returns 0 sections — v1 behavior preserved."""
    preamble, sections = parse_sections("plain body\nno h2 here\n")
    assert sections == []
    assert preamble == "plain body\nno h2 here\n"


def test_parse_sections_extracts_marker_fields():
    body = (
        "## Sillverglass\n"
        '<!-- section: summary="Harbor town." tokens=430 last_updated=2026-04-19 -->\n'
        "\nBody text."
    )
    _, sections = parse_sections(body)
    assert len(sections) == 1
    s = sections[0]
    assert s.slug == "sillverglass"
    assert s.has_marker
    assert s.marker_summary == "Harbor town."
    assert s.marker_tokens == 430
    assert s.marker_last_updated == "2026-04-19"
    assert s.body.startswith("Body text")


def test_parse_sections_multiple():
    body = (
        "Preamble.\n\n## A\n\nBody A.\n\n## B\n<!-- section: summary=\"B summary\" tokens=5 -->\n\nBody B.\n"
    )
    preamble, sections = parse_sections(body)
    assert "Preamble" in preamble
    assert [s.slug for s in sections] == ["a", "b"]
    assert sections[1].marker_summary == "B summary"


def test_parse_sections_handles_escaped_quotes_in_summary():
    body = (
        '## Quoted\n'
        '<!-- section: summary="He said \\"hi\\" briefly." tokens=8 -->\n\n'
        "Body."
    )
    _, sections = parse_sections(body)
    assert sections[0].marker_summary == 'He said "hi" briefly.'


def test_render_summary_view_omits_bodies():
    body = (
        "Preamble.\n\n## A\n<!-- section: summary=\"A is A.\" tokens=3 -->\nA body here.\n\n## B\n\nB body.\n"
    )
    preamble, sections = parse_sections(body)
    view = render_summary_view(preamble, sections)
    assert "A is A." in view
    assert "A body here" not in view  # body omitted in summary view
    assert "## A" in view and "## B" in view


def test_replace_section_body_surgical():
    body = "## One\n\nOriginal one.\n\n## Two\n\nOriginal two.\n"
    new, ok = replace_section_body(body, "two", "Replaced two.")
    assert ok
    assert "Original one" in new
    assert "Replaced two" in new
    assert "Original two" not in new


def test_replace_section_body_unknown_slug():
    body = "## One\n\nBody.\n"
    new, ok = replace_section_body(body, "missing", "x")
    assert not ok
    assert new == body  # unchanged


def test_maybe_refresh_section_tokens_updates_count():
    body = (
        "## Big\n"
        '<!-- section: summary="x" tokens=1 last_updated=2026-01-01 -->\n\n'
        + " ".join(["word"] * 100)
        + "\n"
    )
    refreshed = maybe_refresh_section_tokens(body, last_updated="2026-04-24T00:00:00Z")
    _, sections = parse_sections(refreshed)
    assert sections[0].marker_tokens > 100  # ~130 for 100 words


def test_maybe_refresh_skips_pages_without_markers():
    """Pages with no markers stay unchanged — we don't implicitly add markers."""
    body = "## A\n\nBody without markers.\n"
    assert maybe_refresh_section_tokens(body) == body


# ---- Integration with CanvasService ---------------------------------------


async def test_page_read_full_default_mode(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body="plain body", writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
    )
    assert r.ok
    assert "plain body" in r.extra["body"]
    assert r.extra.get("mode") == "full"


async def test_page_read_summary_mode_no_sections_is_backcompat(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body="plain body with no headings",
        writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        mode="summary",
    )
    assert r.ok
    assert r.extra["section_count"] == 0


async def test_page_read_summary_mode_with_sections(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    body = (
        "Preamble.\n\n"
        "## Alpha\n"
        '<!-- section: summary="Alpha summary." tokens=5 -->\n\n'
        "Alpha body.\n\n"
        "## Beta\n\n"
        "Beta body.\n"
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body=body, writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        mode="summary",
    )
    assert r.ok
    assert r.extra["section_count"] == 2
    assert set(r.extra["section_slugs"]) == {"alpha", "beta"}
    assert "Alpha summary." in r.extra["body"]
    # Body content of the section is NOT in the summary view.
    assert "Alpha body" not in r.extra["body"]


async def test_page_read_section_mode_returns_single_section(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    body = "## Alpha\n\nAlpha body.\n\n## Beta\n\nBeta body.\n"
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body=body, writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        mode="section", section="beta",
    )
    assert r.ok
    assert r.extra["section"] == "beta"
    assert "Beta body" in r.extra["body"]
    assert "Alpha" not in r.extra["body"]


async def test_page_read_section_mode_unknown_slug(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body="## Alpha\n\nA.\n", writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        mode="section", section="missing",
    )
    assert not r.ok
    assert "not found" in r.error.lower()


async def test_page_write_section_targeted(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    body = "## Alpha\n\nAlpha original.\n\n## Beta\n\nBeta original.\n"
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body=body, writer_member_id="alice",
    )
    r = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="Alpha rewritten.", writer_member_id="alice",
        section="alpha",
    )
    assert r.ok
    # Read full to check: alpha replaced, beta preserved.
    rr = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
    )
    assert "Alpha rewritten" in rr.extra["body"]
    assert "Beta original" in rr.extra["body"]
    assert "Alpha original" not in rr.extra["body"]


async def test_page_write_section_unknown_slug_errors(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body="## Alpha\n\nA.\n", writer_member_id="alice",
    )
    r = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="x", writer_member_id="alice", section="missing",
    )
    assert not r.ok
    assert "not found" in r.error.lower()


async def test_page_write_refreshes_tokens_marker(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Test", scope="personal",
    )
    body = (
        "## Big\n"
        '<!-- section: summary="x" tokens=1 last_updated=2026-01-01 -->\n\n'
        + " ".join(["word"] * 100)
        + "\n"
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p", body=body, writer_member_id="alice",
    )
    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        mode="section", section="big",
    )
    assert r.extra["marker_tokens"] > 100
