"""WORKFLOW-PATTERNS-LIBRARY-AND-SEED — seeder + file-inventory guards.

Spec: SPEC-WORKFLOW-PATTERNS-LIBRARY-AND-SEED (2026-04-24).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.instance_db import InstanceDB
from kernos.setup.seed_canvases import seed_canvases_on_first_boot


INSTANCE = "inst_wptest"
OPERATOR = "member:inst_wptest:owner"

EXPECTED_PATTERNS = {
    "00-library-meta.md",
    "01-software-development.md",
    "02-long-form-campaign.md",
    "03-legal-case.md",
    "04-household-management.md",
    "05-time-bounded-event.md",
    "06-per-job-trade-work.md",
    "07-research-lab.md",
    "08-creative-solo.md",
    "09-creative-collective.md",
    "10-client-project.md",
    "11-long-term-client-relationship.md",
    "12-course-development.md",
    "13-investigative-reporting.md",
    "14-open-source-maintenance.md",
    "15-multi-project-operations.md",
    "16-cause-based-organizing.md",
    "17-multi-party-transaction.md",
    "18-personal-long-horizon.md",
}


REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_DIR = REPO_ROOT / "docs" / "workflow-patterns"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    events: list[tuple] = []

    async def emit(iid, et, payload, *, member_id=""):
        events.append((et, payload))

    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path), event_emit=emit)
    yield svc, idb, events, tmp_path
    await idb.close()


# ---- Repo-inventory guards (Pillar 1) -------------------------------------


def test_library_directory_exists():
    assert LIBRARY_DIR.is_dir(), f"{LIBRARY_DIR} missing"


def test_all_19_library_files_present():
    present = {p.name for p in LIBRARY_DIR.glob("*.md")}
    missing = EXPECTED_PATTERNS - present
    extra = present - EXPECTED_PATTERNS
    assert not missing, f"missing: {missing}"
    assert not extra, f"unexpected files: {extra}"


def test_library_files_have_yaml_frontmatter():
    """Each file should open with a ``---`` fence + scope/type/pattern/consumer."""
    for name in EXPECTED_PATTERNS:
        text = (LIBRARY_DIR / name).read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{name} missing YAML frontmatter fence"
        head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
        assert "scope:" in head and "type:" in head and "pattern:" in head, (
            f"{name} frontmatter missing required keys"
        )


def test_library_files_ascii_clean():
    """Hygiene invariant: no curly quotes. Em-dashes retained as in architecture docs."""
    for name in EXPECTED_PATTERNS:
        text = (LIBRARY_DIR / name).read_text(encoding="utf-8")
        for bad, label in (
            ("‘", "curly-left-single"),
            ("’", "curly-right-single"),
            ("“", "curly-left-double"),
            ("”", "curly-right-double"),
        ):
            assert bad not in text, f"{name} contains {label!r}"


# ---- Seeder behavior (Pillar 2) -------------------------------------------


async def test_first_boot_seeds_three_team_canvases_in_order(env):
    svc, idb, events, _ = env
    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    # All three team canvases present in seeded order.
    assert r.seeded_canvases == [
        "System Reference", "Workflow Patterns", "Our Procedures",
    ]
    assert r.skipped_canvases == []


async def test_workflow_patterns_canvas_has_all_19_pages_plus_index(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    wp = await idb.find_canvas_by_name(name="Workflow Patterns", scope="team")
    pages = await svc.page_list(instance_id=INSTANCE, canvas_id=wp["canvas_id"])
    paths = {p["path"] for p in pages}
    # Every pattern file seeded + a landing index.
    for expected in EXPECTED_PATTERNS:
        assert expected in paths, f"missing seeded page: {expected}"
    assert "index.md" in paths
    assert len(pages) == len(EXPECTED_PATTERNS) + 1


async def test_workflow_patterns_owner_is_system(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    wp = await idb.find_canvas_by_name(name="Workflow Patterns", scope="team")
    assert wp["owner_member_id"] == "system"


async def test_second_boot_is_idempotent(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    r2 = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    assert r2.seeded_canvases == []
    assert "Workflow Patterns" in r2.skipped_canvases


async def test_deletion_triggers_reseed_of_workflow_patterns_only(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    wp = await idb.find_canvas_by_name(name="Workflow Patterns", scope="team")
    await idb.archive_canvas(wp["canvas_id"])

    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    assert "Workflow Patterns" in r.seeded_canvases
    assert "System Reference" in r.skipped_canvases
    assert "Our Procedures" in r.skipped_canvases


async def test_missing_docs_dir_skips_workflow_patterns(env, tmp_path):
    svc, idb, _, _ = env
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
        repo_root=empty_root,
    )
    assert "Workflow Patterns" not in r.seeded_canvases
    assert "Our Procedures" in r.seeded_canvases
    assert any("Workflow Patterns" in w for w in r.warnings)


async def test_workflow_patterns_seeded_event_fires_with_page_count(env):
    svc, idb, events, _ = env
    captured = []

    async def capture(iid, et, payload, *, member_id=""):
        captured.append((et, payload))

    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR, event_emit=capture,
    )
    wp_events = [e for e in captured if e[0] == "canvas.seeded" and e[1].get("name") == "Workflow Patterns"]
    assert len(wp_events) == 1
    # 19 pattern files + 1 index page = 20
    assert wp_events[0][1]["pages"] == 20


async def test_pattern_page_preserves_authored_frontmatter(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    wp = await idb.find_canvas_by_name(name="Workflow Patterns", scope="team")
    # Read the software-development pattern and check authored metadata survived.
    pr = await svc.page_read(
        instance_id=INSTANCE, canvas_id=wp["canvas_id"],
        page_slug="01-software-development.md",
    )
    assert pr.ok
    fm = pr.extra["frontmatter"]
    assert fm.get("pattern") == "software-development"
    assert fm.get("consumer") == "gardener"
    assert fm.get("scope") == "team"
