"""CANVAS-V1 Pillar 5 — routes-lite + consult_operator_at inheritance.

Spec reference: SPEC-CANVAS-V1, Pillar 5 expected behaviors.
"""
from __future__ import annotations

import pytest

from kernos.kernel.canvas import (
    CanvasService,
    DEFAULT_CONSULT_OPERATOR_AT,
    classify_route_target,
    parse_route_targets,
    resolve_consult_operator_at,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_routetest"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    events = []

    async def emit(iid, et, payload, *, member_id=""):
        events.append((et, payload))

    svc = CanvasService(
        instance_db=idb, data_dir=str(tmp_path), event_emit=emit,
    )
    yield svc, idb, events
    await idb.close()


# ---- consult_operator_at inheritance ---------------------------------------


def test_consult_page_wins():
    result = resolve_consult_operator_at(
        page_value=["ratified"], canvas_default=["shipped"],
    )
    assert result == ["ratified"]


def test_consult_canvas_wins_when_page_missing():
    result = resolve_consult_operator_at(
        page_value=None, canvas_default=["shipped"],
    )
    assert result == ["shipped"]


def test_consult_falls_to_instance_default():
    result = resolve_consult_operator_at(
        page_value=None, canvas_default=None,
    )
    assert result == list(DEFAULT_CONSULT_OPERATOR_AT)


def test_consult_explicit_empty_is_honored():
    # An explicit [] is a valid override meaning "never consult".
    result = resolve_consult_operator_at(
        page_value=[], canvas_default=["shipped"],
    )
    assert result == []


def test_consult_scalar_string_wraps_to_list():
    result = resolve_consult_operator_at(
        page_value="ratified", canvas_default=None,
    )
    assert result == ["ratified"]


# ---- route target parsing --------------------------------------------------


def test_parse_routes_list_form():
    routes = {"ratified": ["operator", "member:bob"]}
    assert parse_route_targets(routes, "ratified") == ["operator", "member:bob"]


def test_parse_routes_scalar_form():
    assert parse_route_targets({"ratified": "operator"}, "ratified") == ["operator"]


def test_parse_routes_missing_state_returns_empty():
    assert parse_route_targets({"ratified": "operator"}, "proposed") == []


def test_parse_routes_none_returns_empty():
    assert parse_route_targets(None, "ratified") == []


def test_classify_operator():
    assert classify_route_target("operator") == ("operator", "")


def test_classify_member():
    assert classify_route_target("member:alice") == ("member", "alice")


def test_classify_space():
    assert classify_route_target("space:team") == ("space", "team")


def test_classify_unknown():
    assert classify_route_target("bogus") == ("unknown", "bogus")


# ---- integration: routes emerge on state-changed page_write ---------------


async def test_routes_surface_on_state_change(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Decisions", scope="personal",
    )
    # First write: declare routes for the 'ratified' state.
    w1 = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="launch",
        body="Proposing launch.", writer_member_id="alice",
        page_type="decision", state="proposed",
        frontmatter_overrides={"routes": {"ratified": ["operator"]}},
    )
    # state=proposed has no route declared, so route_targets is empty.
    assert w1.extra["route_targets"] == []

    # Transition to ratified — route now fires.
    w2 = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="launch",
        body="Ratified.", writer_member_id="alice",
        page_type="decision", state="ratified",
    )
    assert w2.extra["state_changed"] is True
    assert w2.extra["route_targets"] == ["operator"]


async def test_consult_operator_flag_matches_state(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    # Default consult_operator_at = ('shipped', 'on_conflict').
    # Transitioning to 'shipped' should set consult_operator=True.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice", state="drafted",
    )
    w = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice", state="shipped",
    )
    assert w.extra["consult_operator"] is True


async def test_consult_operator_page_override_suppresses(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    # Page-level consult_operator_at=[] overrides the instance default,
    # so transition to 'shipped' does NOT fire consult_operator.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice", state="drafted",
        frontmatter_overrides={"consult_operator_at": []},
    )
    w = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice", state="shipped",
    )
    assert w.extra["consult_operator"] is False


async def test_events_fire_on_state_change(env):
    svc, _, events = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice", state="drafted",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice", state="current",
    )
    event_types = [e[0] for e in events]
    assert "canvas.created" in event_types
    assert "canvas.page.created" in event_types
    assert "canvas.page.changed" in event_types
    assert "canvas.page.state_changed" in event_types


async def test_archive_event_fires(env):
    svc, _, events = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice", state="drafted",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice", state="archived",
    )
    event_types = [e[0] for e in events]
    assert "canvas.page.archived" in event_types
