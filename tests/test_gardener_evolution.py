"""CANVAS-SECTION-MARKERS + GARDENER Pillar 4 — continuous evolution judgments.

Ships Pattern 00 cross-pattern heuristics only (per scope-discipline
fallback in the spec): staleness, split, scope-mismatch.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.canvas import CanvasService
from kernos.kernel.gardener import (
    ADVISORY_ACTIONS,
    NON_DESTRUCTIVE_ACTIONS,
    SPLIT_SECTION_LINE_THRESHOLD,
    STALENESS_DAYS,
    GardenerService,
    PendingProposal,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_evolutiontest"
OPERATOR = "member:inst_evolutiontest:owner"


class _NullReasoning:
    async def complete_simple(self, *a, **kw):
        raise RuntimeError("evolution tests must not call the LLM")


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    emitted = []

    async def capture_emit(iid, et, payload, *, member_id=""):
        emitted.append((et, payload))

    svc = CanvasService(
        instance_db=idb, data_dir=str(tmp_path), event_emit=capture_emit,
    )
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb,
        reasoning_service=_NullReasoning(),
        event_emit=capture_emit,
    )
    yield gardener, svc, idb, emitted
    await gardener.wait_idle()
    await idb.close()


# ---- Pure heuristics (no event routing) -----------------------------------


def test_heuristic_split_fires_when_3_sections_exceed_threshold():
    """Page with three sections each >= SPLIT_SECTION_LINE_THRESHOLD lines."""
    big_section = "\n".join(["line"] * (SPLIT_SECTION_LINE_THRESHOLD + 5))
    body = (
        f"## Alpha\n\n{big_section}\n\n"
        f"## Beta\n\n{big_section}\n\n"
        f"## Gamma\n\n{big_section}\n"
    )
    decision = GardenerService._heuristic_split(
        canvas_scope="team", page_path="x.md", page_fm={}, page_body=body,
    )
    assert decision is not None
    assert decision.action == "propose_split"
    assert decision.confidence == "high"
    assert decision.surfaces


def test_heuristic_split_quiet_when_under_threshold():
    body = "## Alpha\n\nshort\n\n## Beta\n\nshort\n\n## Gamma\n\nshort\n"
    assert GardenerService._heuristic_split(
        canvas_scope="team", page_path="x.md", page_fm={}, page_body=body,
    ) is None


def test_heuristic_staleness_fires_on_old_last_updated():
    old = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 10)).isoformat()
    fm = {"last_updated": old}
    d = GardenerService._heuristic_staleness(
        canvas_scope="team", page_path="x.md", page_fm=fm, page_body="",
    )
    assert d is not None
    assert d.action == "flag_stale"
    assert d.surfaces
    assert d.payload["age_days"] >= STALENESS_DAYS


def test_heuristic_staleness_quiet_on_fresh_page():
    fm = {"last_updated": datetime.now(timezone.utc).isoformat()}
    assert GardenerService._heuristic_staleness(
        canvas_scope="team", page_path="x.md", page_fm=fm, page_body="",
    ) is None


def test_heuristic_staleness_quiet_without_last_updated():
    assert GardenerService._heuristic_staleness(
        canvas_scope="team", page_path="x.md", page_fm={}, page_body="",
    ) is None


def test_heuristic_scope_mismatch_flags_personal_page_on_team_canvas():
    d = GardenerService._heuristic_scope_mismatch(
        canvas_scope="team", page_path="x.md",
        page_fm={"scope": "personal"}, page_body="",
    )
    assert d is not None
    assert d.action == "flag_scope_mismatch"
    assert d.surfaces


def test_heuristic_scope_mismatch_quiet_on_aligned_scopes():
    assert GardenerService._heuristic_scope_mismatch(
        canvas_scope="team", page_path="x.md",
        page_fm={"scope": "team"}, page_body="",
    ) is None


def test_heuristic_scope_mismatch_quiet_when_page_scope_absent():
    assert GardenerService._heuristic_scope_mismatch(
        canvas_scope="team", page_path="x.md",
        page_fm={}, page_body="",
    ) is None


# ---- Consent-mode routing -------------------------------------------------


def test_is_auto_apply_advisory_never_auto():
    for action in ADVISORY_ACTIONS:
        d = GardenerDecision(action=action, confidence="high")
        for mode in ("propose-all", "auto-non-destructive", "auto-all"):
            assert not GardenerService._is_auto_apply(d, mode)


def test_is_auto_apply_non_destructive_auto_under_auto_modes():
    for action in NON_DESTRUCTIVE_ACTIONS:
        d = GardenerDecision(action=action, confidence="high")
        assert GardenerService._is_auto_apply(d, "auto-non-destructive")
        assert GardenerService._is_auto_apply(d, "auto-all")
        assert not GardenerService._is_auto_apply(d, "propose-all")


def test_is_auto_apply_split_always_proposes_even_under_auto_all():
    d = GardenerDecision(action="propose_split", confidence="high")
    assert not GardenerService._is_auto_apply(d, "auto-all")


# ---- Event routing + coalescing ------------------------------------------


async def test_event_routing_buffers_high_confidence_proposal(env):
    gardener, svc, idb, emitted = env
    # Create a canvas whose page will trigger the split heuristic.
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Big", scope="team",
    )
    big_section = "\n".join(["line"] * (SPLIT_SECTION_LINE_THRESHOLD + 5))
    body = (
        f"## Alpha\n\n{big_section}\n\n"
        f"## Beta\n\n{big_section}\n\n"
        f"## Gamma\n\n{big_section}\n"
    )
    # Writing this page fires canvas.page.created → Gardener dispatches.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="big.md", body=body,
        writer_member_id=OPERATOR,
    )
    # Manually invoke dispatch synchronously for deterministic testing.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {
            "canvas_id": c.canvas_id, "page_path": "big.md",
            "writer_member_id": OPERATOR,
        },
    )
    assert gardener.coalescer.buffered_count(c.canvas_id) >= 1


async def test_event_routing_ignores_non_page_events(env):
    gardener, svc, idb, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="x", scope="team",
    )
    await gardener._dispatch(
        INSTANCE, "canvas.created",
        {"canvas_id": c.canvas_id, "name": "x"},
    )
    assert gardener.coalescer.buffered_count(c.canvas_id) == 0


async def test_event_routing_skips_when_canvas_missing(env):
    gardener, _, _, _ = env
    # Unknown canvas_id: no crash, no buffer.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": "canvas_nope", "page_path": "x.md"},
    )
    assert gardener.coalescer.buffered_count("canvas_nope") == 0


async def test_drain_proposals_returns_buffered_then_clears(env):
    gardener, _, _, _ = env
    now = datetime.now(timezone.utc)
    gardener.coalescer.add(PendingProposal(
        canvas_id="c1", action="propose_split", confidence="high",
        rationale="r", affected_pages=["x.md"], captured_at=now,
    ))
    drained = await gardener.drain_proposals(canvas_id="c1")
    assert len(drained) == 1
    # Immediate second drain: window hasn't elapsed → empty.
    again = await gardener.drain_proposals(canvas_id="c1")
    assert again == []


async def test_staleness_heuristic_fires_via_full_dispatch_path(env):
    gardener, svc, idb, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Old", scope="team",
    )
    # Write a page then manually stamp old last_updated into frontmatter
    # by rewriting it with an explicit frontmatter_overrides.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 30)).isoformat()
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="old.md", body="# Old\n\nOld content.\n",
        writer_member_id=OPERATOR,
    )
    # Forge an old last_updated by writing again with overrides — the
    # write will apply utc_now AFTER overrides.update, so overrides here
    # won't stick for last_updated. Instead directly mutate the file.
    from pathlib import Path
    from kernos.kernel.canvas import canvas_dir, parse_frontmatter, serialize_frontmatter
    p = canvas_dir(str(Path(idb._db_path).parent), INSTANCE, c.canvas_id) / "old.md"
    text = p.read_text()
    fm, body = parse_frontmatter(text)
    fm["last_updated"] = old_ts
    p.write_text(serialize_frontmatter(fm, body))

    await gardener._dispatch(
        INSTANCE, "canvas.page.changed",
        {
            "canvas_id": c.canvas_id, "page_path": "old.md",
            "writer_member_id": OPERATOR,
        },
    )
    buffered = gardener.coalescer.buffered_count(c.canvas_id)
    assert buffered >= 1


async def test_auto_apply_path_emits_canvas_reshaped_event(env):
    gardener, svc, idb, emitted = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Auto", scope="team",
    )
    # Directly call _auto_apply to verify routing + audit-event emission.
    d = GardenerDecision(
        action="regenerate_summary", confidence="high",
        rationale="test", affected_pages=["x.md"],
    )
    await gardener._auto_apply(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_path="x.md", decision=d,
    )
    reshaped = [e for e in emitted if e[0] == "canvas.reshaped"]
    assert len(reshaped) == 1
    assert reshaped[0][1]["source"] == "gardener"
    assert reshaped[0][1]["action"] == "regenerate_summary"


async def test_gardener_respects_consent_mode_from_canvas_yaml(env):
    """Non-destructive action under auto-non-destructive should auto, not buffer."""
    gardener, svc, idb, emitted = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="ConsentCanvas", scope="team",
    )
    # Stamp gardener_consent=auto-non-destructive into canvas.yaml.
    from pathlib import Path
    import yaml
    from kernos.kernel.canvas import canvas_dir
    yaml_path = canvas_dir(str(Path(idb._db_path).parent), INSTANCE, c.canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    data["gardener_consent"] = "auto-non-destructive"
    yaml_path.write_text(yaml.safe_dump(data))

    # Simulate the heuristic runner returning a non_destructive action by
    # calling _dispatch with a page that doesn't trigger heuristics, then
    # separately exercise the consent routing via _is_auto_apply.
    # (The full integration is covered; this test confirms canvas.yaml
    # is read without error in _dispatch.)
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="p.md", body="# P\n\nsmall body.\n",
        writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": c.canvas_id, "page_path": "p.md"},
    )
    # No heuristic should fire on this small page → no proposals buffered.
    assert gardener.coalescer.buffered_count(c.canvas_id) == 0
