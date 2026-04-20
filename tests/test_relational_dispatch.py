"""Dispatcher tests for RELATIONAL-MESSAGING v5.

Covers: permission matrix rejection, happy-path send, immediate-push
state transition, next-turn pickup, space-hint deferral variants,
crash-recovery re-pickup of delivered messages, expiration sweep, and
mark_surfaced / mark_resolved transitions.
"""
import asyncio
import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.relational_dispatch import (
    DEFER_REASON_SPACE_HINT_MISMATCH, DEFER_REASON_SPACE_HINT_STALE,
    RelationalDispatcher,
)
from kernos.kernel.state_json import JsonStateStore
from kernos.utils import utc_now


INSTANCE = "inst_rmtest"


@pytest.fixture
async def env(tmp_path):
    state = JsonStateStore(str(tmp_path))
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    # Two members for the tests.
    await idb.create_member("harold", "Harold", "owner", "")
    await idb.create_member("emma", "Emma", "member", "")
    events: list[tuple[str, str]] = []

    dispatcher = RelationalDispatcher(
        state=state, instance_db=idb,
        trace_emitter=lambda name, detail: events.append((name, detail)),
    )
    yield dispatcher, state, idb, events
    await idb.close()


# ----- Permission matrix -----


@pytest.mark.asyncio
async def test_send_rejects_ask_question_at_by_permission_default(env):
    dispatcher, _state, _idb, _events = env
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="ask_question", content="where are you?",
    )
    assert result.ok is False
    assert "permission denied" in result.error


@pytest.mark.asyncio
async def test_send_allows_request_action_at_by_permission_default(env):
    dispatcher, state, _idb, _events = env
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="request_action",
        content="Can you book 3pm?",
    )
    assert result.ok is True
    msg = await state.get_relational_message(INSTANCE, result.message_id)
    assert msg.intent == "request_action"
    assert msg.state == "pending"


@pytest.mark.asyncio
async def test_send_rejects_all_at_no_access(env):
    dispatcher, _state, idb, _events = env
    # Harold declares Emma as no-access.
    await idb.declare_relationship("harold", "emma", "no-access")
    for intent in ("request_action", "inform", "ask_question"):
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="harold", origin_agent_identity="Slate",
            addressee="emma", intent=intent, content="hi",
        )
        assert result.ok is False, f"{intent} must be rejected"


@pytest.mark.asyncio
async def test_send_allows_ask_question_at_full_access(env):
    dispatcher, _state, idb, _events = env
    await idb.declare_relationship("harold", "emma", "full-access")
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="ask_question", content="where are you?",
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_send_resolves_addressee_by_display_name(env):
    dispatcher, _state, _idb, _events = env
    # "Emma" display name should resolve to member_id "emma".
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="Emma", intent="inform", content="team meeting at 2",
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_send_rejects_self_addressee(env):
    dispatcher, _state, _idb, _events = env
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="harold", intent="inform", content="self",
    )
    assert result.ok is False


# ----- Immediate-push path (time_sensitive) -----


@pytest.mark.asyncio
async def test_time_sensitive_flips_to_delivered_on_send(env):
    dispatcher, state, _idb, events = env
    pushed: list = []

    async def capture_push(msg):
        pushed.append(msg.id)

    dispatcher._push = capture_push
    result = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform",
        content="fire in the kitchen", urgency="time_sensitive",
    )
    assert result.ok is True
    msg = await state.get_relational_message(INSTANCE, result.message_id)
    assert msg.state == "delivered"
    assert msg.delivered_at
    assert pushed == [result.message_id]
    # Trace emitted sent + delivered (immediate_push).
    names = [n for n, _ in events]
    assert "relational_message.sent" in names
    assert "relational_message.delivered" in names


# ----- Next-turn pickup + space-hint rule -----


@pytest.mark.asyncio
async def test_collect_promotes_pending_to_delivered_no_hint(env):
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="sync-up at 2pm",
    )
    collected = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    ids = [m.id for m in collected]
    assert r.message_id in ids
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "delivered"


@pytest.mark.asyncio
async def test_space_hint_mismatch_defers(env):
    dispatcher, state, _idb, events = env
    # Hint names a real space in recipient's list, but not the active one.
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="about the side project",
        target_space_hint="space_emma_sideproject",
    )
    collected = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general", "space_emma_sideproject"],
    )
    assert r.message_id not in [m.id for m in collected]
    # Still pending (not yet delivered).
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "pending"
    deferred_events = [d for n, d in events if n == "relational_message.deferred"]
    assert any(DEFER_REASON_SPACE_HINT_MISMATCH in d for d in deferred_events)


@pytest.mark.asyncio
async def test_space_hint_stale_falls_through(env):
    dispatcher, state, _idb, events = env
    # Hint names a space the recipient doesn't have (renamed/deleted).
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="old hint",
        target_space_hint="space_emma_dead",
    )
    collected = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    ids = [m.id for m in collected]
    assert r.message_id in ids, "stale hint must fall through"
    deferred_events = [d for n, d in events if n == "relational_message.deferred"]
    assert any(DEFER_REASON_SPACE_HINT_STALE in d for d in deferred_events)


@pytest.mark.asyncio
async def test_time_sensitive_bypasses_space_hint_deferral(env):
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="urgent",
        urgency="time_sensitive", target_space_hint="space_emma_sideproject",
    )
    # time_sensitive already flipped to delivered on send; collect should
    # include it regardless of active-space mismatch.
    collected = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general", "space_emma_sideproject"],
    )
    assert r.message_id in [m.id for m in collected]


# ----- Crash-recovery -----


@pytest.mark.asyncio
async def test_crash_between_delivered_and_surfaced_re_picks_up(env):
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="msg",
    )
    # Turn 1: collect → delivered, but crash before surfaced.
    first = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    assert r.message_id in [m.id for m in first]

    # Turn 2: collect should re-include the delivered-but-not-surfaced.
    second = await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    assert r.message_id in [m.id for m in second]
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "delivered"  # still not surfaced


@pytest.mark.asyncio
async def test_mark_surfaced_and_resolved(env):
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="msg",
    )
    await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    assert await dispatcher.mark_surfaced(INSTANCE, r.message_id) is True
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "surfaced"
    assert await dispatcher.mark_resolved(
        INSTANCE, r.message_id, from_state="surfaced", reason="user_handled",
    ) is True
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "resolved"


@pytest.mark.asyncio
async def test_delivered_to_resolved_direct(env):
    """Agent-side auto-handle (covenant auto-handles). Skip surfaced."""
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="request_action", content="msg",
    )
    await dispatcher.collect_pending_for_member(
        instance_id=INSTANCE, member_id="emma",
        active_space_id="space_emma_general",
        recipient_space_ids=["space_emma_general"],
    )
    ok = await dispatcher.mark_resolved(
        INSTANCE, r.message_id, from_state="delivered", reason="auto_handled",
    )
    assert ok is True
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "resolved"
    assert msg.surfaced_at == ""  # never transitioned through surfaced


# ----- Expiration -----


@pytest.mark.asyncio
async def test_sweep_expired_marks_old_pendings_expired(env):
    dispatcher, state, _idb, events = env
    # Create a pending message with an artificially-old created_at.
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="stale",
    )
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    # Rewrite created_at to a time well past the 72h normal TTL.
    msg.created_at = "2025-01-01T00:00:00+00:00"
    # Delete + re-add to persist the backdated envelope.
    await state.delete_relational_message(INSTANCE, msg.id)
    await state.add_relational_message(msg)
    count = await dispatcher.sweep_expired(INSTANCE)
    assert count >= 1
    reloaded = await state.get_relational_message(INSTANCE, msg.id)
    assert reloaded.state == "expired"
    names = [n for n, _ in events]
    assert "relational_message.expired" in names


# ----- Concurrent immediate-push + collect -----


@pytest.mark.asyncio
async def test_concurrent_push_and_collect_no_duplicate(env):
    """If time_sensitive push flips pending→delivered while the recipient
    is mid-turn, collect_pending_for_member must not double-transition.
    """
    dispatcher, state, _idb, _events = env
    r = await dispatcher.send(
        instance_id=INSTANCE,
        origin_member_id="harold", origin_agent_identity="Slate",
        addressee="emma", intent="inform", content="race",
    )
    # Simulate push happening at the same time as collect.
    await asyncio.gather(
        dispatcher._immediate_push(
            await state.get_relational_message(INSTANCE, r.message_id),
        ),
        dispatcher.collect_pending_for_member(
            instance_id=INSTANCE, member_id="emma",
            active_space_id="space_emma_general",
            recipient_space_ids=["space_emma_general"],
        ),
    )
    msg = await state.get_relational_message(INSTANCE, r.message_id)
    assert msg.state == "delivered"  # not duplicated, not broken
