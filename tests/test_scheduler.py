"""Tests for SPEC-3E-B: Time-Triggered Scheduler.

Covers: Trigger dataclass, TriggerStore persistence, manage_schedule tool,
trigger evaluation, time helpers, gate classification.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.scheduler import (
    MANAGE_SCHEDULE_TOOL,
    Trigger,
    TriggerStore,
    compute_next_fire,
    evaluate_triggers,
    handle_manage_schedule,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _future_iso(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past_iso(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Trigger dataclass
# ---------------------------------------------------------------------------


class TestTriggerDataclass:
    def test_defaults(self):
        t = Trigger(trigger_id="trig_test", tenant_id="t1")
        assert t.status == "active"
        assert t.action_type == "notify"
        assert t.fire_count == 0
        assert t.delivery_class == "stage"


# ---------------------------------------------------------------------------
# TriggerStore
# ---------------------------------------------------------------------------


class TestTriggerStore:
    async def test_save_and_get(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_1", tenant_id="t1", action_description="test")
        await store.save(t)
        loaded = await store.get("t1", "trig_1")
        assert loaded is not None
        assert loaded.action_description == "test"

    async def test_list_active(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", tenant_id="t", status="active"))
        await store.save(Trigger(trigger_id="t2", tenant_id="t", status="completed"))
        active = await store.list_active("t")
        assert len(active) == 1
        assert active[0].trigger_id == "t1"

    async def test_list_all(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", tenant_id="t", status="active"))
        await store.save(Trigger(trigger_id="t2", tenant_id="t", status="completed"))
        all_triggers = await store.list_all("t")
        assert len(all_triggers) == 2

    async def test_get_due(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(
            trigger_id="due", tenant_id="t", status="active",
            next_fire_at=_past_iso(1),
        ))
        await store.save(Trigger(
            trigger_id="future", tenant_id="t", status="active",
            next_fire_at=_future_iso(1),
        ))
        due = await store.get_due("t", _now_iso())
        assert len(due) == 1
        assert due[0].trigger_id == "due"

    async def test_remove(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", tenant_id="t"))
        assert await store.remove("t", "t1")
        assert await store.get("t", "t1") is None

    async def test_remove_nonexistent(self, tmp_path):
        store = TriggerStore(tmp_path)
        assert not await store.remove("t", "nope")

    async def test_upsert(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="t1", tenant_id="t", action_description="v1")
        await store.save(t)
        t.action_description = "v2"
        await store.save(t)
        loaded = await store.get("t", "t1")
        assert loaded.action_description == "v2"
        all_triggers = await store.list_all("t")
        assert len(all_triggers) == 1

    async def test_empty_store(self, tmp_path):
        store = TriggerStore(tmp_path)
        assert await store.list_all("t") == []
        assert await store.get_due("t", _now_iso()) == []


# ---------------------------------------------------------------------------
# manage_schedule tool
# ---------------------------------------------------------------------------


class TestManageScheduleTool:
    def test_tool_shape(self):
        assert MANAGE_SCHEDULE_TOOL["name"] == "manage_schedule"
        schema = MANAGE_SCHEDULE_TOOL["input_schema"]
        assert "action" in schema["properties"]
        assert "create" in schema["properties"]["action"]["enum"]

    def test_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "manage_schedule" in ReasoningService._KERNEL_TOOLS

    def test_gate_classification_list(self):
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect("manage_schedule", None, {"action": "list"}) == "read"

    def test_gate_classification_create_notify(self):
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect(
            "manage_schedule", None, {"action": "create", "action_type": "notify"}
        ) == "read"

    def test_gate_classification_create_tool_call(self):
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect(
            "manage_schedule", None, {"action": "create", "action_type": "tool_call"}
        ) == "soft_write"

    def test_gate_classification_remove(self):
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect(
            "manage_schedule", None, {"action": "remove"}
        ) == "soft_write"


class TestManageScheduleHandler:
    async def test_list_empty(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(store, "t1", "", "", "list")
        assert "No scheduled" in result

    async def test_create_notify(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(
            store, "t1", "m1", "s1", "create",
            description="Check email",
            when=_future_iso(1),
            action_type="notify",
            message="Time to check your email!",
        )
        assert "Scheduled" in result
        assert "trig_" in result
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        assert triggers[0].action_type == "notify"

    async def test_create_missing_when(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", description="test",
        )
        assert "Error" in result

    async def test_create_missing_description(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", when=_future_iso(1),
        )
        assert "Error" in result

    async def test_pause_and_resume(self, tmp_path):
        store = TriggerStore(tmp_path)
        await handle_manage_schedule(
            store, "t1", "", "", "create",
            description="test", when=_future_iso(1),
        )
        triggers = await store.list_active("t1")
        tid = triggers[0].trigger_id

        result = await handle_manage_schedule(store, "t1", "", "", "pause", trigger_id=tid)
        assert "paused" in result
        assert len(await store.list_active("t1")) == 0

        result = await handle_manage_schedule(store, "t1", "", "", "resume", trigger_id=tid)
        assert "active" in result
        assert len(await store.list_active("t1")) == 1

    async def test_remove(self, tmp_path):
        store = TriggerStore(tmp_path)
        await handle_manage_schedule(
            store, "t1", "", "", "create",
            description="test", when=_future_iso(1),
        )
        triggers = await store.list_all("t1")
        tid = triggers[0].trigger_id

        result = await handle_manage_schedule(store, "t1", "", "", "remove", trigger_id=tid)
        assert "Removed" in result
        assert await store.get("t1", tid) is None

    async def test_list_shows_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        await handle_manage_schedule(
            store, "t1", "", "", "create",
            description="Check email", when=_future_iso(1),
        )
        result = await handle_manage_schedule(store, "t1", "", "", "list")
        assert "Check email" in result
        assert "notify" in result


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


class TestTimeHelpers:
    def test_compute_next_fire_cron(self):
        now = _now_iso()
        result = compute_next_fire("0 9 * * 1", now)  # Every Monday 9am
        assert result != ""
        assert result > now

    def test_compute_next_fire_invalid(self):
        result = compute_next_fire("not a cron", _now_iso())
        assert result == ""


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------


class TestTriggerEvaluation:
    async def test_fires_due_notify(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_due", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Check email",
            action_params={"message": "Time to check email!"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1

        handler.send_outbound.assert_called_once()
        updated = await store.get("t1", "trig_due")
        assert updated.status == "completed"
        assert updated.fire_count == 1

    async def test_skips_future_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_future", tenant_id="t1", status="active",
            next_fire_at=_future_iso(1),
        )
        await store.save(t)

        handler = MagicMock()
        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 0

    async def test_recurring_recomputes(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_recur", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            recurrence="0 9 * * *",  # Daily at 9am
            action_type="notify",
            action_description="Daily check",
            action_params={"message": "Good morning!"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1

        updated = await store.get("t1", "trig_recur")
        assert updated.status == "active"  # Still active (recurring)
        assert updated.next_fire_at > _now_iso()  # Recomputed to future

    async def test_outbound_failure_sets_pending(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_fail", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Test",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_fail")
        assert updated.pending_delivery == "Hello"
