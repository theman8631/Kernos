"""CANVAS-SECTION-MARKERS + GARDENER Pillar 3 — initial-shape judgment.

Covers:
  - Pattern body parser (parse_initial_shape)
  - GardenerService.apply_initial_shape with explicit pattern bypass
  - GardenerService.apply_initial_shape via Gardener LLM pick (stubbed)
  - Unmatched fallback (pattern: unmatched recorded in canvas.yaml)
  - Unknown explicit pattern falls back gracefully
  - Idempotency on re-application (pages already exist)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.canvas import (
    CanvasService,
    canvas_dir,
    parse_initial_shape,
)
from kernos.kernel.gardener import GardenerService, WORKFLOW_PATTERNS_CANVAS_NAME
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_initialshape"
OPERATOR = "member:inst_initialshape:owner"


# ---- Parser ---------------------------------------------------------------


def test_parse_initial_shape_from_software_development_pattern():
    """Real-world parse — the committed software-development pattern."""
    body = (Path("docs/workflow-patterns/01-software-development.md")
            .read_text(encoding="utf-8"))
    specs = parse_initial_shape(body)
    paths = {s["path"] for s in specs}
    assert "charter.md" in paths
    assert "architecture.md" in paths
    assert "ledger.md" in paths
    assert any(s["type"] == "log" for s in specs)


def test_parse_initial_shape_returns_empty_when_section_absent():
    body = "## Dials\n\nNo shape section here.\n"
    assert parse_initial_shape(body) == []


def test_parse_initial_shape_skips_subdirectory_bullets():
    """Bullets like `specs/` (no file extension, trailing slash) are skipped."""
    body = (
        "## Initial canvas shape\n\n"
        "- `concrete.md` (note, team) — real page\n"
        "- `specs/` — subdirectory hint, not a file\n"
    )
    specs = parse_initial_shape(body)
    assert len(specs) == 1
    assert specs[0]["path"] == "concrete.md"


def test_parse_initial_shape_captures_flags():
    body = (
        "## Initial canvas shape\n\n"
        "- `ledger.md` (log, team, append-only) — decisions\n"
    )
    specs = parse_initial_shape(body)
    assert specs[0]["flags"] == ["append-only"]


def test_parse_initial_shape_defaults_type_to_note_and_scope_to_team():
    body = "## Initial canvas shape\n\n- `bare.md` — a page without meta\n"
    specs = parse_initial_shape(body)
    assert specs[0]["type"] == "note"
    assert specs[0]["scope"] == "team"


# ---- Gardener integration: explicit pattern --------------------------------


class _ReasoningFixed:
    """Returns a pre-canned decision for each consult_simple call.

    Used by tests where we don't want to exercise the LLM path but still
    want to verify the Gardener's post-decision actions.
    """

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_simple(self, *, system_prompt, user_content, chain,
                               output_schema=None, max_tokens=1024):
        self.calls.append({
            "system": system_prompt, "user": user_content, "chain": chain,
        })
        return json.dumps(self.payload) if self.payload else ""


@pytest.fixture
async def seeded_env(tmp_path):
    """Instance with a Workflow Patterns canvas pre-seeded, ready for Gardener."""
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    # Seed a stripped Workflow Patterns canvas with one real pattern.
    wp = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name=WORKFLOW_PATTERNS_CANVAS_NAME, scope="team",
    )
    # Copy the real software-development pattern in as a page.
    body = (Path("docs/workflow-patterns/01-software-development.md")
            .read_text(encoding="utf-8"))
    # Strip the existing YAML frontmatter so page_write's frontmatter layer
    # wins.
    from kernos.kernel.canvas import parse_frontmatter
    fm, stripped = parse_frontmatter(body)
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=wp.canvas_id,
        page_slug="01-software-development.md",
        body=stripped, writer_member_id=OPERATOR,
        title="Software Development", page_type="note", state="current",
        frontmatter_overrides={"pattern": "software-development"},
    )
    yield svc, idb, tmp_path
    await idb.close()


async def test_apply_initial_shape_with_explicit_pattern(seeded_env):
    svc, idb, tmp_path = seeded_env
    reasoning = _ReasoningFixed()  # should never be called
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=reasoning,
    )

    # Create a fresh target canvas to receive the pattern.
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="New Project", scope="personal",
    )
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="New Project",
        scope="personal",
        creator_member_id=OPERATOR,
        explicit_pattern="software-development",
    )

    assert result["source"] == "explicit"
    assert result["pattern"] == "software-development"
    assert result["pages_created"] >= 5  # at least the bulk of the pattern's pages
    # LLM must not have been consulted for explicit-pattern path.
    assert reasoning.calls == []

    # canvas.yaml records the pattern.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, target.canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    assert data["pattern"] == "software-development"

    # At least a couple of pattern-declared pages exist on disk.
    pages = await svc.page_list(instance_id=INSTANCE, canvas_id=target.canvas_id)
    paths = {p["path"] for p in pages}
    assert "charter.md" in paths
    assert "architecture.md" in paths


async def test_apply_initial_shape_via_gardener_pick(seeded_env):
    svc, idb, tmp_path = seeded_env
    reasoning = _ReasoningFixed({
        "action": "pick_pattern",
        "confidence": "high",
        "pattern": "software-development",
        "rationale": "clear dial match",
    })
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=reasoning,
    )
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Build X", scope="personal",
    )
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="Build X",
        scope="personal",
        creator_member_id=OPERATOR,
        intent="building a backend service with specs and a decision ledger",
    )
    assert result["source"] == "gardener"
    assert result["pattern"] == "software-development"
    assert result["pages_created"] >= 1
    assert len(reasoning.calls) == 1  # LLM consulted exactly once


async def test_apply_initial_shape_unmatched_falls_back_to_unmatched_flag(seeded_env):
    svc, idb, tmp_path = seeded_env
    # LLM returns action=none — pattern not pickable.
    reasoning = _ReasoningFixed({"action": "none", "confidence": "low"})
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=reasoning,
    )
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Mystery", scope="personal",
    )
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="Mystery",
        scope="personal",
        creator_member_id=OPERATOR,
        intent="something nothing matches",
    )
    assert result["pattern"] == "unmatched"
    assert result["pages_created"] == 0
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, target.canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    assert data["pattern"] == "unmatched"


async def test_apply_initial_shape_unknown_explicit_pattern_falls_back(seeded_env):
    svc, idb, tmp_path = seeded_env
    reasoning = _ReasoningFixed()
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=reasoning,
    )
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Misnamed", scope="personal",
    )
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="Misnamed",
        scope="personal",
        creator_member_id=OPERATOR,
        explicit_pattern="this-pattern-does-not-exist",
    )
    # Unknown explicit pattern → falls through to consultation path; with
    # no intent, the Gardener's LLM stub returns nothing → unmatched.
    assert result["pattern"] == "unmatched"
    assert result["pages_created"] == 0


async def test_apply_initial_shape_idempotent_on_existing_pages(seeded_env):
    """Re-applying a pattern over an existing instantiation doesn't duplicate pages."""
    svc, idb, tmp_path = seeded_env
    reasoning = _ReasoningFixed()
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=reasoning,
    )
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Twice", scope="personal",
    )
    r1 = await gardener.apply_initial_shape(
        instance_id=INSTANCE, canvas_id=target.canvas_id,
        canvas_name="Twice", scope="personal", creator_member_id=OPERATOR,
        explicit_pattern="software-development",
    )
    first_count = r1["pages_created"]
    assert first_count >= 1

    r2 = await gardener.apply_initial_shape(
        instance_id=INSTANCE, canvas_id=target.canvas_id,
        canvas_name="Twice", scope="personal", creator_member_id=OPERATOR,
        explicit_pattern="software-development",
    )
    # Second application: every page already exists → no new creations.
    assert r2["pages_created"] == 0
