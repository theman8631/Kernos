"""SYSTEM-REFERENCE-CANVAS-SEED — first-boot seeder + idempotency.

Spec: SPEC-SYSTEM-REFERENCE-CANVAS-SEED (2026-04-23).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.instance_db import InstanceDB
from kernos.setup.seed_canvases import (
    SYSTEM_OWNER,
    SYSTEM_REFERENCE_SEED_MAP,
    append_my_tools_page,
    seed_canvases_on_first_boot,
    seed_my_tools_canvas_for_member,
)


INSTANCE = "inst_seedtest"
OPERATOR = "member:inst_seedtest:owner"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    events: list[tuple] = []

    async def emit(iid, et, payload, *, member_id=""):
        events.append((et, payload))

    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path), event_emit=emit)
    yield svc, idb, events, Path(tmp_path)
    await idb.close()


async def test_first_boot_seeds_two_team_canvases(env):
    svc, idb, events, _ = env
    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    assert "System Reference" in r.seeded_canvases
    assert "Our Procedures" in r.seeded_canvases
    assert r.skipped_canvases == []
    # index (overwritten) + 7 concept pages + 1 tools index + 1 procedures index
    assert r.pages_written >= 9


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
    assert "System Reference" in r2.skipped_canvases
    assert "Our Procedures" in r2.skipped_canvases
    assert r2.seeded_canvases == []
    assert r2.pages_written == 0


async def test_deletion_triggers_reseed(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    # Archive the system reference canvas.
    sr = await idb.find_canvas_by_name(name="System Reference", scope="team")
    await idb.archive_canvas(sr["canvas_id"])
    # Re-seeding should now create a fresh System Reference.
    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    assert "System Reference" in r.seeded_canvases
    assert "Our Procedures" in r.skipped_canvases


async def test_system_reference_seed_pages_present(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    sr = await idb.find_canvas_by_name(name="System Reference", scope="team")
    pages = await svc.page_list(instance_id=INSTANCE, canvas_id=sr["canvas_id"])
    paths = {p["path"] for p in pages}
    # Every configured seed target should have been written.
    for target in SYSTEM_REFERENCE_SEED_MAP.values():
        assert target in paths, f"missing seeded page: {target}"
    assert "tools/kernel-tools.md" in paths
    assert "index.md" in paths


async def test_system_reference_owner_is_system(env):
    svc, idb, _, _ = env
    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
    )
    sr = await idb.find_canvas_by_name(name="System Reference", scope="team")
    assert sr["owner_member_id"] == SYSTEM_OWNER


async def test_missing_docs_dir_skips_system_reference(env, tmp_path):
    svc, idb, _, _ = env
    # Point at an empty directory — no docs/architecture inside.
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    r = await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
        repo_root=empty_root,
    )
    # System Reference should be skipped with a warning; Our Procedures still seeds.
    assert "System Reference" not in r.seeded_canvases
    assert "Our Procedures" in r.seeded_canvases
    assert any("System Reference" in w for w in r.warnings)


async def test_my_tools_per_member_creation(env):
    svc, idb, events, _ = env
    member = "member:alice"
    await idb.create_member(member, "Alice", "member", "")
    r = await seed_my_tools_canvas_for_member(
        instance_id=INSTANCE, member_id=member,
        canvas_service=svc, instance_db=idb,
    )
    assert "My Tools" in r.seeded_canvases
    assert r.pages_written == 1
    # Idempotent
    r2 = await seed_my_tools_canvas_for_member(
        instance_id=INSTANCE, member_id=member,
        canvas_service=svc, instance_db=idb,
    )
    assert "My Tools" in r2.skipped_canvases
    assert r2.pages_written == 0


async def test_my_tools_personal_scope_not_visible_to_others(env):
    svc, idb, _, _ = env
    a = "member:alice"
    b = "member:bob"
    await idb.create_member(a, "Alice", "member", "")
    await idb.create_member(b, "Bob", "member", "")
    await seed_my_tools_canvas_for_member(
        instance_id=INSTANCE, member_id=a,
        canvas_service=svc, instance_db=idb,
    )
    alice_canvases = await svc.list_for_member(member_id=a)
    bob_canvases = await svc.list_for_member(member_id=b)
    alice_names = {c["name"] for c in alice_canvases}
    bob_names = {c["name"] for c in bob_canvases}
    assert "My Tools" in alice_names
    assert "My Tools" not in bob_names


async def test_append_my_tools_page_noop_without_canvas(env):
    svc, idb, _, _ = env
    # No My Tools canvas for this member yet — append should no-op.
    ok = await append_my_tools_page(
        instance_id=INSTANCE, member_id="member:nobody",
        tool_name="example", descriptor={"name": "example", "description": "d"},
        canvas_service=svc, instance_db=idb,
    )
    assert ok is False


async def test_append_my_tools_page_writes_when_canvas_exists(env):
    svc, idb, _, _ = env
    member = "member:alice"
    await idb.create_member(member, "Alice", "member", "")
    await seed_my_tools_canvas_for_member(
        instance_id=INSTANCE, member_id=member,
        canvas_service=svc, instance_db=idb,
    )
    descriptor = {
        "name": "invoice_tracker",
        "description": "Track invoices",
        "input_schema": {"type": "object", "properties": {"amount": {"type": "number"}}},
        "implementation": "invoice_tracker.py",
        "effects": "append-only",
    }
    ok = await append_my_tools_page(
        instance_id=INSTANCE, member_id=member,
        tool_name="invoice_tracker", descriptor=descriptor,
        canvas_service=svc, instance_db=idb,
    )
    assert ok is True
    # Verify the page landed.
    canvas = await idb.find_canvas_by_name(
        name="My Tools", scope="personal", owner_member_id=member,
    )
    pages = await svc.page_list(
        instance_id=INSTANCE, canvas_id=canvas["canvas_id"],
    )
    assert any(p["path"] == "tools/invoice_tracker.md" for p in pages)


async def test_seed_emits_canvas_seeded_events(env):
    svc, idb, events, _ = env

    async def capture_seeded(iid, et, payload, *, member_id=""):
        events.append((et, payload.get("name")))

    await seed_canvases_on_first_boot(
        INSTANCE, canvas_service=svc, instance_db=idb,
        operator_member_id=OPERATOR,
        event_emit=capture_seeded,
    )
    seeded_events = [e for e in events if e[0] == "canvas.seeded"]
    names = {e[1] for e in seeded_events}
    assert "System Reference" in names
    assert "Our Procedures" in names
