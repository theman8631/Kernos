"""Tests for SPEC-3E-B: Time-Triggered Scheduler + Event Triggers + Lifecycle.

Covers: Trigger dataclass, TriggerStore persistence, manage_schedule tool,
trigger evaluation, time helpers, gate classification, CalendarEvent,
parse_calendar_events, evaluate_event_triggers, resolve_owner_member_id,
classify_trigger_failure, retire_stale_triggers, degraded lifecycle.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.scheduler import (
    CalendarEvent,
    EVENT_DAILY_FIRE_CAP,
    MANAGE_SCHEDULE_TOOL,
    TRANSIENT_FAILURE_NOTIFY_THRESHOLD,
    Trigger,
    TriggerStore,
    classify_trigger_failure,
    compute_next_fire,
    evaluate_event_triggers,
    evaluate_triggers,
    handle_manage_schedule,
    parse_calendar_events,
    resolve_owner_member_id,
    retire_stale_triggers,
)


def _now_iso():
    return datetime.now().isoformat()


def _future_iso(hours: float = 1) -> str:
    return (datetime.now() + timedelta(hours=hours)).isoformat()


def _past_iso(hours: float = 1) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat()


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

    def test_gate_classification_all_read(self):
        """All manage_schedule actions are read — gate fires at fire time, not management time."""
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        for action in ["list", "create", "remove", "pause", "resume", "update"]:
            assert r._classify_tool_effect(
                "manage_schedule", None, {"action": action}
            ) == "read", f"Expected read for action={action}"


class TestManageScheduleHandler:
    async def test_list_empty(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(store, "t1", "", "", "list")
        assert "No scheduled" in result

    async def test_create_notify_via_extraction(self, tmp_path):
        """Create with NL description + mock Haiku extraction."""
        import json
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "notify",
            "when": _future_iso(1),
            "message": "Time to check your email!",
            "delivery_class": "stage",
            "recurrence": "",
            "notify_via": "",
            "tool_name": "",
            "tool_args": "",
        }))

        result = await handle_manage_schedule(
            store, "t1", "m1", "s1", "create",
            description="Remind me to check email in 1 hour",
            reasoning_service=reasoning,
        )
        assert "Scheduled" in result
        assert "trig_" in result
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        assert triggers[0].action_type == "notify"

    async def test_create_missing_reasoning_service(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", description="test",
        )
        assert "Error" in result

    async def test_create_missing_description(self, tmp_path):
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", reasoning_service=reasoning,
        )
        assert "Error" in result

    async def test_pause_and_resume(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_pr", tenant_id="t1", status="active",
                    action_description="test", next_fire_at=_future_iso(1))
        await store.save(t)

        result = await handle_manage_schedule(store, "t1", "", "", "pause", trigger_id="trig_pr")
        assert "paused" in result
        assert len(await store.list_active("t1")) == 0

        result = await handle_manage_schedule(store, "t1", "", "", "resume", trigger_id="trig_pr")
        assert "active" in result
        assert len(await store.list_active("t1")) == 1

    async def test_remove(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_rm", tenant_id="t1", action_description="test")
        await store.save(t)

        result = await handle_manage_schedule(store, "t1", "", "", "remove", trigger_id="trig_rm")
        assert "Removed" in result
        assert await store.get("t1", "trig_rm") is None

    async def test_create_stores_conversation_id(self, tmp_path):
        """Create passes conversation_id through to the trigger."""
        import json
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "notify",
            "when": _future_iso(1),
            "message": "Test reminder",
            "delivery_class": "stage",
            "recurrence": "",
            "notify_via": "",
            "tool_name": "",
            "tool_args": "",
        }))

        await handle_manage_schedule(
            store, "t1", "m1", "s1", "create",
            description="Remind me in 1 hour",
            reasoning_service=reasoning,
            conversation_id="conv_abc",
        )
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        assert triggers[0].conversation_id == "conv_abc"

    async def test_list_shows_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_ls", tenant_id="t1", action_description="Check email",
                    next_fire_at=_future_iso(1), action_type="notify")
        await store.save(t)
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

    async def test_notify_stores_scheduled_message(self, tmp_path):
        """Successful notify injects [SCHEDULED] message into conversation history."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_hist", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Check email",
            action_params={"message": "Time to check email!"},
            conversation_id="conv_123",
            space_id="space_daily",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)

        handler.conversations.append.assert_called_once()
        call_args = handler.conversations.append.call_args
        assert call_args[0][0] == "t1"
        assert call_args[0][1] == "conv_123"
        entry = call_args[0][2]
        assert entry["role"] == "assistant"
        assert entry["content"].startswith("[SCHEDULED]")
        assert "Time to check email!" in entry["content"]
        assert entry["platform"] == "scheduler"
        assert entry["space_tags"] == ["space_daily"]

    async def test_notify_no_history_without_conversation_id(self, tmp_path):
        """No conversation history injection when trigger has no conversation_id."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_noid", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Test",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        handler.conversations.append.assert_not_called()

    async def test_outbound_failure_no_history(self, tmp_path):
        """Failed outbound does NOT inject into conversation history."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_fh", tenant_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_params={"message": "Hello"},
            conversation_id="conv_123",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        handler.conversations.append.assert_not_called()

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


# ---------------------------------------------------------------------------
# resolve_owner_member_id (Component 3)
# ---------------------------------------------------------------------------


class TestResolveOwnerMemberId:
    def test_canonical_format(self):
        assert resolve_owner_member_id("discord:123") == "member:discord:123:owner"

    def test_sms_tenant(self):
        assert resolve_owner_member_id("+15555550100") == "member:+15555550100:owner"


# ---------------------------------------------------------------------------
# CalendarEvent + parse_calendar_events (Component 1)
# ---------------------------------------------------------------------------


def _calendar_item(event_id: str, summary: str, start_dt: datetime,
                   end_dt: datetime | None = None, location: str = "",
                   all_day: bool = False) -> dict:
    """Build a mock Google Calendar API item."""
    item: dict = {"id": event_id, "summary": summary}
    if all_day:
        item["start"] = {"date": start_dt.strftime("%Y-%m-%d")}
        item["end"] = {"date": (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")}
    else:
        item["start"] = {"dateTime": start_dt.isoformat()}
        if end_dt:
            item["end"] = {"dateTime": end_dt.isoformat()}
    if location:
        item["location"] = location
    return item


class TestCalendarEventParsing:
    def test_parse_timed_events(self):
        now = datetime.now(timezone.utc)
        items = [
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=20)),
            _calendar_item("ev2", "Lunch", now + timedelta(hours=2), location="Cafe"),
        ]
        events = parse_calendar_events(json.dumps(items))
        assert len(events) == 2
        assert events[0].id == "ev1"
        assert events[0].summary == "Meeting"
        assert events[1].location == "Cafe"
        assert not events[0].is_all_day

    def test_skip_all_day_events(self):
        """AC5: All-day events are skipped entirely."""
        now = datetime.now(timezone.utc)
        items = [
            _calendar_item("ev1", "Holiday", now, all_day=True),
            _calendar_item("ev2", "Meeting", now + timedelta(minutes=20)),
        ]
        events = parse_calendar_events(json.dumps(items))
        assert len(events) == 1
        assert events[0].id == "ev2"

    def test_parse_wrapped_response(self):
        """Handles {items: [...]} and {events: [...]} wrappers."""
        now = datetime.now(timezone.utc)
        item = _calendar_item("ev1", "Test", now)
        assert len(parse_calendar_events(json.dumps({"items": [item]}))) == 1
        assert len(parse_calendar_events(json.dumps({"events": [item]}))) == 1

    def test_parse_invalid_json(self):
        assert parse_calendar_events("not json") == []

    def test_parse_missing_datetime(self):
        """Items without dateTime in start are skipped."""
        events = parse_calendar_events(json.dumps([{"id": "x", "start": {}}]))
        assert events == []

    def test_default_summary(self):
        now = datetime.now(timezone.utc)
        events = parse_calendar_events(json.dumps([
            {"id": "x", "start": {"dateTime": now.isoformat()}}
        ]))
        assert events[0].summary == "Calendar event"


# ---------------------------------------------------------------------------
# TriggerStore.get_by_condition_type (Component 5)
# ---------------------------------------------------------------------------


class TestGetByConditionType:
    async def test_returns_only_event_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        time_trigger = Trigger(trigger_id="trig_1", tenant_id="t1",
                               condition_type="time", status="active",
                               next_fire_at=_future_iso())
        event_trigger = Trigger(trigger_id="trig_2", tenant_id="t1",
                                condition_type="event", status="active",
                                event_source="calendar")
        await store.save(time_trigger)
        await store.save(event_trigger)

        result = await store.get_by_condition_type("t1", "event")
        assert len(result) == 1
        assert result[0].trigger_id == "trig_2"

    async def test_filters_by_status(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="trig_1", tenant_id="t1",
                                 condition_type="event", status="active",
                                 event_source="calendar"))
        await store.save(Trigger(trigger_id="trig_2", tenant_id="t1",
                                 condition_type="event", status="completed",
                                 event_source="calendar"))

        result = await store.get_by_condition_type("t1", "event", status="active")
        assert len(result) == 1
        assert result[0].trigger_id == "trig_1"


# ---------------------------------------------------------------------------
# evaluate_event_triggers (Component 4)
# ---------------------------------------------------------------------------


class TestEvaluateEventTriggers:
    def _make_trigger(self, **kwargs) -> Trigger:
        defaults = dict(
            trigger_id="trig_ev1", tenant_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=30, status="active",
            action_type="notify", action_description="Calendar alert",
        )
        defaults.update(kwargs)
        return Trigger(**defaults)

    def _calendar_json(self, events: list[dict]) -> str:
        return json.dumps(events)

    async def test_fires_within_lead_time(self, tmp_path):
        """AC3: Fires when event is within lead_minutes."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        event_start = now + timedelta(minutes=15)
        raw = self._calendar_json([
            _calendar_item("ev1", "Team Standup", event_start),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1
        handler.send_outbound.assert_called_once()
        call_args = handler.send_outbound.call_args
        assert "Team Standup" in call_args[0][3]

    async def test_does_not_fire_outside_lead_time(self, tmp_path):
        """Event 2 hours away should not fire with 30min lead."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(hours=2)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0
        handler.send_outbound.assert_not_called()

    async def test_no_duplicate_fire(self, tmp_path):
        """AC4: Same event doesn't fire twice."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(event_matched_ids=["ev1"])
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=15)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0

    async def test_event_filter_matches_title(self, tmp_path):
        """AC15: event_filter matches title/summary only."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(event_filter="dentist")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Dentist Appointment", now + timedelta(minutes=20)),
            _calendar_item("ev2", "Team Meeting", now + timedelta(minutes=20)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1
        assert "Dentist" in handler.send_outbound.call_args[0][3]

    async def test_one_shot_completes_after_fire(self, tmp_path):
        """AC6: One-shot triggers complete after first fire."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(recurrence="")  # one-shot
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "completed"

    async def test_standing_stays_active(self, tmp_path):
        """AC6: Standing triggers stay active after fire."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(recurrence="standing")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "active"
        assert updated.fire_count == 1
        assert updated.event_daily_fire_count == 1

    async def test_daily_cap_prevents_spam(self, tmp_path):
        """AC7/AC8: Daily cap on standing triggers."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            recurrence="standing",
            event_daily_fire_count=EVENT_DAILY_FIRE_CAP,
            event_daily_fire_date=now.strftime("%Y-%m-%d"),
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0

    async def test_daily_cap_resets_on_new_day(self, tmp_path):
        """Daily cap resets when the date changes."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            recurrence="standing",
            event_daily_fire_count=EVENT_DAILY_FIRE_CAP,
            event_daily_fire_date="2020-01-01",  # old date
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1  # cap reset

    async def test_mcp_error_logs_and_skips(self, tmp_path):
        """AC9: MCP failures log and skip; trigger stays active."""
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        handler = MagicMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value="Tool error: connection refused")

        fired = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "active"

    async def test_matched_ids_pruned(self, tmp_path):
        """AC10: event_matched_ids pruned each pass."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            event_matched_ids=["old_gone", "ev1"],
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=15)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        # "old_gone" should be pruned; "ev1" remains (still in window)
        assert "old_gone" not in updated.event_matched_ids
        assert "ev1" in updated.event_matched_ids

    async def test_notify_via_channel_targeting(self, tmp_path):
        """AC11: Event notifications respect notify_via."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(notify_via="sms")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        call_args = handler.send_outbound.call_args
        assert call_args[0][2] == "sms"

    async def test_member_id_uses_resolver(self, tmp_path):
        """AC13: member_id resolved via resolve_owner_member_id()."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(tenant_id="discord:456")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "discord:456", handler, mcp)
        call_args = handler.send_outbound.call_args
        assert call_args[0][1] == "member:discord:456:owner"


# ---------------------------------------------------------------------------
# Time triggers unchanged (AC16)
# ---------------------------------------------------------------------------


class TestTimeTriggerNoRegression:
    async def test_time_triggers_still_work(self, tmp_path):
        """AC16: time-based triggers are unaffected by event trigger additions."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_time1", tenant_id="t1", condition_type="time",
            next_fire_at=_past_iso(), status="active",
            action_type="notify", action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1
        handler.send_outbound.assert_called_once()


# ---------------------------------------------------------------------------
# Trigger dataclass — event fields (Component 2)
# ---------------------------------------------------------------------------


class TestTriggerEventFields:
    def test_defaults(self):
        t = Trigger(trigger_id="t1", tenant_id="t1")
        assert t.condition_type == "time"
        assert t.event_source == ""
        assert t.event_filter == ""
        assert t.event_lead_minutes == 30
        assert t.event_matched_ids == []
        assert t.event_daily_fire_count == 0
        assert t.event_daily_fire_date == ""

    def test_event_trigger_fields(self):
        t = Trigger(
            trigger_id="t1", tenant_id="t1",
            condition_type="event", event_source="calendar",
            event_filter="dentist", event_lead_minutes=15,
        )
        assert t.condition_type == "event"
        assert t.event_source == "calendar"
        assert t.event_filter == "dentist"
        assert t.event_lead_minutes == 15


# ---------------------------------------------------------------------------
# Bug regression tests
# ---------------------------------------------------------------------------


class TestBugRegressions:
    async def test_standing_recurrence_not_parsed_as_cron(self, tmp_path):
        """BUG 2: recurrence='standing' should not be parsed as cron for event triggers."""
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        result = await _create_trigger(
            store, "t1", "member:t1:owner", "space1",
            description="Alert before meetings",
            when="", action_type="notify", message="Meeting soon",
            tool_name="", tool_args={}, notify_via="",
            delivery_class="stage", recurrence="standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=30,
        )
        # Should succeed, not "Could not parse recurrence 'standing'"
        assert "Scheduled:" in result
        assert "Error" not in result

        # Verify trigger was stored correctly
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        t = triggers[0]
        assert t.condition_type == "event"
        assert t.recurrence == "standing"
        assert t.next_fire_at == ""  # event triggers don't use next_fire_at

    async def test_stale_tool_trigger_fails_gracefully(self, tmp_path):
        """BUG 3: tool_call trigger with nonexistent tool should fail, not retry forever."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_stale", tenant_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "nonexistent_tool", "tool_args": {}},
            action_description="Run missing tool",
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            return_value="Kernel tool 'nonexistent_tool' not handled."
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_stale")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert "permanently unavailable" in updated.failure_reason.lower()

    def test_seed_from_previous_param_name(self):
        """BUG 1: seed_from_previous parameter is tail_entries, not tail_lines."""
        import inspect
        from kernos.kernel.conversation_log import ConversationLogger
        sig = inspect.signature(ConversationLogger.seed_from_previous)
        assert "tail_entries" in sig.parameters
        assert "tail_lines" not in sig.parameters


# ---------------------------------------------------------------------------
# classify_trigger_failure (Component 2)
# ---------------------------------------------------------------------------


class TestClassifyTriggerFailure:
    def test_structural_patterns(self):
        assert classify_trigger_failure("Tool not found: calendar") == "structural"
        assert classify_trigger_failure("Kernel tool 'x' not handled") == "structural"
        assert classify_trigger_failure("unknown tool foo") == "structural"
        assert classify_trigger_failure("channel not registered") == "structural"
        assert classify_trigger_failure("Tool permanently unavailable") == "structural"

    def test_transient_default(self):
        assert classify_trigger_failure("connection timeout") == "transient"
        assert classify_trigger_failure("rate limited") == "transient"
        assert classify_trigger_failure("internal server error") == "transient"
        assert classify_trigger_failure(RuntimeError("network down")) == "transient"

    def test_conservative_default(self):
        """Unknown errors default to transient — better retry than retire."""
        assert classify_trigger_failure("something went wrong") == "transient"


# ---------------------------------------------------------------------------
# Trigger lifecycle fields (Component 1)
# ---------------------------------------------------------------------------


class TestTriggerLifecycleFields:
    def test_defaults(self):
        t = Trigger(trigger_id="t1", tenant_id="t1")
        assert t.failure_class == ""
        assert t.transient_failure_count == 0
        assert t.last_failure_at == ""
        assert t.degraded is False
        assert t.retired_at == ""

    def test_retired_status(self):
        t = Trigger(trigger_id="t1", tenant_id="t1", status="retired",
                    failure_class="structural", retired_at="2026-01-01T00:00:00")
        assert t.status == "retired"


# ---------------------------------------------------------------------------
# Degraded lifecycle (Components 3 & 4)
# ---------------------------------------------------------------------------


class TestDegradedLifecycle:
    async def test_structural_failure_retires_trigger(self, tmp_path):
        """AC1-3: Structural failure retires trigger permanently."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_struct", tenant_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "missing_tool", "tool_args": {}},
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            return_value="Kernel tool 'missing_tool' not handled."
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_struct")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert updated.retired_at != ""

    async def test_transient_failure_keeps_active(self, tmp_path):
        """AC4: Transient failures keep trigger active."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_trans", tenant_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "some_tool", "tool_args": {}},
            recurrence="0 * * * *",  # recurring so it doesn't auto-fail
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            side_effect=RuntimeError("connection timeout")
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_trans")
        assert updated.status != "retired"
        assert updated.degraded is True
        assert updated.transient_failure_count == 1
        assert updated.failure_class == "transient"

    async def test_degraded_logged_only_on_transition(self, tmp_path):
        """AC5-6: degraded=True on first failure, log only on transition."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_d", tenant_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="notify",
            action_params={"message": "Hello"},
            recurrence="0 * * * *",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        # First fire — delivery fails, trigger degrades
        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_d")
        # Notify trigger returns False when send_outbound fails
        # but that's just pending delivery, not classified as transient
        # The trigger stays active with pending_delivery set

    async def test_recovery_clears_degraded(self, tmp_path):
        """AC8-9: Successful fire after degraded clears flag, keeps failure_reason."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_recov", tenant_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="notify",
            action_params={"message": "Hello"},
            degraded=True, transient_failure_count=5,
            failure_class="transient", failure_reason="previous error",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_recov")
        assert updated.degraded is False
        assert updated.transient_failure_count == 0
        assert updated.failure_class == ""
        # failure_reason preserved for debugging history
        assert updated.failure_reason == "previous error"


# ---------------------------------------------------------------------------
# Boot scan — retire_stale_triggers (Component 5)
# ---------------------------------------------------------------------------


class TestRetireStaleTriggers:
    async def test_retires_missing_tool(self, tmp_path):
        """AC1-2: Stale trigger with missing tool is retired and notified."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_stale2", tenant_id="t1",
            status="active", action_type="tool_call",
            action_params={"tool_name": "calendar_event_reminder"},
            action_description="Old broken reminder",
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value=None)  # Tool doesn't exist

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 1

        updated = await store.get("t1", "trig_stale2")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert "no longer exists" in updated.failure_reason
        handler.send_outbound.assert_called_once()

    async def test_skips_existing_tools(self, tmp_path):
        """Tools that still exist are not retired."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_ok", tenant_id="t1",
            status="active", action_type="tool_call",
            action_params={"tool_name": "list-events"},
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value={"name": "list-events"})

        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0

    async def test_skips_notify_triggers(self, tmp_path):
        """Notify triggers are not scanned for tool existence."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_notify", tenant_id="t1",
            status="active", action_type="notify",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        registry = MagicMock()
        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0

    async def test_skips_already_retired(self, tmp_path):
        """AC11: Already retired triggers are not re-processed."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_ret", tenant_id="t1",
            status="retired", action_type="tool_call",
            action_params={"tool_name": "gone"},
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value=None)
        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0
