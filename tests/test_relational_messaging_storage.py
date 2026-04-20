"""Storage-layer tests for RELATIONAL-MESSAGING envelopes.

The single most important invariant: `transition_relational_message_state`
is genuinely atomic. Each backend's CAS path is exercised explicitly.
"""
import asyncio
import pytest

from kernos.kernel.relational_messaging import (
    RelationalMessage, generate_message_id, generate_conversation_id,
    dispatch_permitted,
)
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.state_sqlite import SqliteStateStore
from kernos.utils import utc_now


def _make_message(instance_id="inst_test", state="pending"):
    return RelationalMessage(
        id=generate_message_id(),
        instance_id=instance_id,
        origin_member_id="harold",
        origin_agent_identity="Slate",
        addressee_member_id="emma",
        intent="ask_question",
        content="Are you free at 3pm?",
        urgency="normal",
        conversation_id=generate_conversation_id(),
        state=state,
        created_at=utc_now(),
    )


# ---------- Dispatch permission matrix (pure) ----------

def test_dispatch_permission_no_access_rejects_all():
    for intent in ("request_action", "inform", "ask_question"):
        assert dispatch_permitted("no-access", intent) is False


def test_dispatch_permission_full_access_allows_all():
    for intent in ("request_action", "inform", "ask_question"):
        assert dispatch_permitted("full-access", intent) is True


def test_dispatch_permission_by_permission_rejects_ask_question_only():
    assert dispatch_permitted("by-permission", "request_action") is True
    assert dispatch_permitted("by-permission", "inform") is True
    assert dispatch_permitted("by-permission", "ask_question") is False


def test_dispatch_permission_missing_treated_as_by_permission():
    # Empty / unknown / None all fall through to by-permission default.
    for missing in ("", None, "unknown"):
        assert dispatch_permitted(missing, "ask_question") is False
        assert dispatch_permitted(missing, "request_action") is True


# ---------- JSON backend atomicity ----------


@pytest.mark.asyncio
async def test_json_add_get_query(tmp_path):
    store = JsonStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)
    got = await store.get_relational_message(msg.instance_id, msg.id)
    assert got is not None
    assert got.id == msg.id
    assert got.state == "pending"

    found = await store.query_relational_messages(
        msg.instance_id, addressee_member_id="emma",
    )
    assert any(m.id == msg.id for m in found)


@pytest.mark.asyncio
async def test_json_transition_cas_winner_wins_loser_sees_false(tmp_path):
    store = JsonStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)

    # First transition succeeds.
    now = utc_now()
    ok1 = await store.transition_relational_message_state(
        msg.instance_id, msg.id,
        from_state="pending", to_state="delivered",
        updates={"delivered_at": now},
    )
    assert ok1 is True

    # A concurrent second caller with the same from_state must lose.
    ok2 = await store.transition_relational_message_state(
        msg.instance_id, msg.id,
        from_state="pending", to_state="delivered",
    )
    assert ok2 is False

    # Storage reflects the winner's update exactly once.
    got = await store.get_relational_message(msg.instance_id, msg.id)
    assert got.state == "delivered"
    assert got.delivered_at == now


@pytest.mark.asyncio
async def test_json_transition_wrong_from_state_returns_false(tmp_path):
    store = JsonStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)

    # Trying to go delivered → surfaced when state is still pending fails.
    ok = await store.transition_relational_message_state(
        msg.instance_id, msg.id,
        from_state="delivered", to_state="surfaced",
    )
    assert ok is False
    got = await store.get_relational_message(msg.instance_id, msg.id)
    assert got.state == "pending"


@pytest.mark.asyncio
async def test_json_concurrent_transitions_only_one_wins(tmp_path):
    """Race a bunch of tasks all trying pending → delivered.

    Only one task should see True. This is the property the state
    machine relies on. If more than one succeeds, delivery duplicates.
    """
    store = JsonStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)

    async def try_transition():
        return await store.transition_relational_message_state(
            msg.instance_id, msg.id,
            from_state="pending", to_state="delivered",
        )

    results = await asyncio.gather(*(try_transition() for _ in range(10)))
    assert sum(1 for r in results if r) == 1
    assert sum(1 for r in results if not r) == 9


# ---------- SQLite backend atomicity ----------


@pytest.mark.asyncio
async def test_sqlite_add_get_query(tmp_path):
    store = SqliteStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)
    got = await store.get_relational_message(msg.instance_id, msg.id)
    assert got is not None
    assert got.id == msg.id
    await store.close_all()


@pytest.mark.asyncio
async def test_sqlite_transition_cas_winner_wins(tmp_path):
    store = SqliteStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)

    now = utc_now()
    ok1 = await store.transition_relational_message_state(
        msg.instance_id, msg.id,
        from_state="pending", to_state="delivered",
        updates={"delivered_at": now},
    )
    assert ok1 is True

    ok2 = await store.transition_relational_message_state(
        msg.instance_id, msg.id,
        from_state="pending", to_state="delivered",
    )
    assert ok2 is False

    got = await store.get_relational_message(msg.instance_id, msg.id)
    assert got.state == "delivered"
    assert got.delivered_at == now
    await store.close_all()


@pytest.mark.asyncio
async def test_sqlite_concurrent_transitions_only_one_wins(tmp_path):
    store = SqliteStateStore(str(tmp_path))
    msg = _make_message()
    await store.add_relational_message(msg)

    async def try_transition():
        return await store.transition_relational_message_state(
            msg.instance_id, msg.id,
            from_state="pending", to_state="delivered",
        )

    results = await asyncio.gather(*(try_transition() for _ in range(10)))
    assert sum(1 for r in results if r) == 1
    assert sum(1 for r in results if not r) == 9
    await store.close_all()
