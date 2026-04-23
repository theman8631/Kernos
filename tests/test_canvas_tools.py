"""CANVAS-V1 Pillar 3 — tool dispatch + consent-on-cross-member-writes.

Spec reference: SPEC-CANVAS-V1, Pillar 3 expected behaviors.
Exercises the _handle_canvas_tool dispatch in ReasoningService — but only
the layer above service calls (consent gate + input shape), not the full
turn pipeline.
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.disclosure_gate import filter_canvases_by_membership
from kernos.kernel.gate import DispatchGate
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.reasoning import ReasoningService


INSTANCE = "inst_tooldispatchtest"


class _StubProvider:
    """Minimal provider stub — never actually called by these tests."""


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    # Handler stub: reasoning looks it up via self._handler._instance_db /
    # self._handler._get_relational_dispatcher() / ._get_canvas_service().
    handler = types.SimpleNamespace()
    handler._instance_db = idb

    def _get_canvas():
        return svc

    def _get_dispatcher():
        return None

    handler._get_canvas_service = _get_canvas
    handler._get_relational_dispatcher = _get_dispatcher

    reasoning = ReasoningService(provider=_StubProvider(), events=None, mcp=None)
    reasoning._handler = handler
    reasoning._canvas = svc
    yield reasoning, svc, idb
    await idb.close()


def _request(instance_id=INSTANCE, member_id="alice", active_space="default"):
    # ReasoningRequest-like stub (dataclass wouldn't buy anything here).
    return types.SimpleNamespace(
        instance_id=instance_id, member_id=member_id,
        active_space_id=active_space, conversation_id="",
        user_timezone="UTC", trace=None,
    )


# ---- Gate classifications --------------------------------------------------


def test_gate_classifies_canvas_reads():
    gate = DispatchGate(reasoning_service=None, registry=None, state=None, events=None, mcp=None)
    assert gate.classify_tool_effect("canvas_list", None, {}) == "read"
    assert gate.classify_tool_effect("page_read", None, {}) == "read"
    assert gate.classify_tool_effect("page_list", None, {}) == "read"
    assert gate.classify_tool_effect("page_search", None, {}) == "read"


def test_gate_classifies_canvas_writes():
    gate = DispatchGate(reasoning_service=None, registry=None, state=None, events=None, mcp=None)
    assert gate.classify_tool_effect("page_write", None, {}) == "soft_write"
    assert gate.classify_tool_effect("canvas_create", None, {}) == "hard_write"


# ---- canvas_create -> offer-less personal -----------------------------


async def test_canvas_create_via_tool(env):
    reasoning, svc, _ = env
    req = _request()
    out = await reasoning._handle_canvas_tool(
        "canvas_create",
        {"name": "MyCanvas", "scope": "personal"},
        req,
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["canvas_id"].startswith("canvas_")


# ---- canvas_list via tool --------------------------------------------


async def test_canvas_list_via_tool(env):
    reasoning, svc, _ = env
    await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="X", scope="personal",
    )
    out = await reasoning._handle_canvas_tool(
        "canvas_list", {}, _request(),
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert len(payload["canvases"]) == 1


# ---- access enforcement on page_read / page_list / page_write ----------


async def test_page_read_denied_for_non_member(env):
    reasoning, svc, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Alice's", scope="personal",
    )
    out = await reasoning._handle_canvas_tool(
        "page_read",
        {"canvas_id": c.canvas_id, "page_path": "index.md"},
        _request(member_id="bob"),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload.get("error") == "canvas_not_accessible"


# ---- consent gate on cross-member writes -----------------------------


async def test_page_write_requires_confirmation_on_shared_canvas(env):
    reasoning, svc, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Shared", scope="specific", members=["bob"],
    )
    # First call without confirmed=true → requires_confirmation.
    out = await reasoning._handle_canvas_tool(
        "page_write",
        {
            "canvas_id": c.canvas_id, "page_path": "note",
            "body": "proposed text", "page_type": "note",
        },
        _request(),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["requires_confirmation"] is True
    assert "bob" in payload["other_members"]


async def test_page_write_proceeds_when_confirmed(env):
    reasoning, svc, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Shared", scope="specific", members=["bob"],
    )
    out = await reasoning._handle_canvas_tool(
        "page_write",
        {
            "canvas_id": c.canvas_id, "page_path": "note",
            "body": "text", "page_type": "note", "confirmed": True,
        },
        _request(),
    )
    payload = json.loads(out)
    assert payload["ok"] is True


async def test_page_write_log_type_skips_consent(env):
    reasoning, svc, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Team", scope="team",
    )
    # Log pages are append-only by convention — no consent required even
    # though the canvas is team scope.
    out = await reasoning._handle_canvas_tool(
        "page_write",
        {
            "canvas_id": c.canvas_id, "page_path": "timeline",
            "body": "entry", "page_type": "log",
        },
        _request(),
    )
    payload = json.loads(out)
    assert payload["ok"] is True


async def test_page_write_personal_canvas_skips_consent(env):
    reasoning, svc, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Personal", scope="personal",
    )
    out = await reasoning._handle_canvas_tool(
        "page_write",
        {
            "canvas_id": c.canvas_id, "page_path": "p",
            "body": "x", "page_type": "note",
        },
        _request(),
    )
    payload = json.loads(out)
    assert payload["ok"] is True


# ---- Disclosure gate filter --------------------------------------------


def test_filter_canvases_team_passes_everyone():
    canvases = [
        {"canvas_id": "c1", "scope": "team"},
        {"canvas_id": "c2", "scope": "personal"},
    ]
    kept = filter_canvases_by_membership(
        canvases, requesting_member_id="nobody",
        canvas_member_lookup=lambda _: [],
    )
    # Only team survives.
    ids = [c["canvas_id"] for c in kept]
    assert ids == ["c1"]


def test_filter_canvases_explicit_member_passes():
    canvases = [
        {"canvas_id": "c1", "scope": "specific"},
    ]
    kept = filter_canvases_by_membership(
        canvases, requesting_member_id="alice",
        canvas_member_lookup=lambda cid: ["alice", "bob"] if cid == "c1" else [],
    )
    assert len(kept) == 1


def test_filter_canvases_fails_closed_on_exception():
    canvases = [{"canvas_id": "c1", "scope": "specific"}]

    def _bad(_cid):
        raise RuntimeError("boom")

    kept = filter_canvases_by_membership(
        canvases, requesting_member_id="alice", canvas_member_lookup=_bad,
    )
    assert kept == []
