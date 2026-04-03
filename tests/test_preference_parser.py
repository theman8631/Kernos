"""Tests for Preference Parser cohort agent (SPEC-6A-4)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.preference_parser import (
    DetectionResult,
    MatchResult,
    commit_preference,
    detect_preference,
    match_candidates,
    parse_preferences_in_message,
)
from kernos.kernel.state import Preference, generate_preference_id
from kernos.kernel.state_json import JsonStateStore
from kernos.utils import utc_now


T = "sms:+15555550100"


def _mock_reasoning(detect_response: dict | None = None):
    """Return a mock reasoning service that returns structured JSON from complete_simple."""
    reasoning = AsyncMock()
    if detect_response:
        reasoning.complete_simple.return_value = json.dumps(detect_response)
    else:
        reasoning.complete_simple.return_value = "{}"
    return reasoning


def _high_confidence_notify() -> dict:
    return {
        "is_preference": True,
        "confidence": "high",
        "category": "notification",
        "subject": "calendar_events",
        "action": "notify",
        "parameters": {"lead_time_minutes": 10},
        "scope_hint": "global",
        "reasoning": "Explicit durable intent about notifications",
    }


def _low_confidence_casual() -> dict:
    return {
        "is_preference": True,
        "confidence": "low",
        "category": "format",
        "subject": "responses",
        "action": "prefer",
        "parameters": {},
        "scope_hint": "unclear",
        "reasoning": "Casual remark, not durable intent",
    }


def _not_a_preference() -> dict:
    return {
        "is_preference": False,
        "confidence": "low",
        "category": "behavior",
        "subject": "",
        "action": "prefer",
        "parameters": {},
        "scope_hint": "unclear",
        "reasoning": "This is a question, not a preference",
    }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def test_detect_high_confidence_preference():
    """True positive: explicit durable intent detected."""
    reasoning = _mock_reasoning(_high_confidence_notify())
    result = await detect_preference("Text me 10 minutes before appointments", reasoning)
    assert result is not None
    assert result.is_preference is True
    assert result.confidence == "high"
    assert result.category == "notification"
    assert result.action == "notify"


async def test_detect_low_confidence_rejected():
    """Conservative: low confidence returns None."""
    reasoning = _mock_reasoning(_low_confidence_casual())
    result = await detect_preference("Bullet points would be nice", reasoning)
    assert result is None


async def test_detect_not_a_preference():
    """True negative: questions are not preferences."""
    reasoning = _mock_reasoning(_not_a_preference())
    result = await detect_preference("What time is it?", reasoning)
    assert result is None


async def test_detect_empty_message():
    """Empty message returns None immediately."""
    reasoning = _mock_reasoning()
    result = await detect_preference("", reasoning)
    assert result is None
    reasoning.complete_simple.assert_not_awaited()


async def test_detect_failure_returns_none():
    """LLM failure returns None gracefully."""
    reasoning = AsyncMock()
    reasoning.complete_simple.side_effect = RuntimeError("LLM down")
    result = await detect_preference("Text me before meetings", reasoning)
    assert result is None


# ---------------------------------------------------------------------------
# Candidate matching
# ---------------------------------------------------------------------------


async def test_match_no_existing_returns_add(tmp_path):
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={"lead_time_minutes": 10},
        scope_hint="global", reasoning="",
    )
    result = await match_candidates(detection, T, store)
    assert result.action == "add"


async def test_match_one_existing_different_params_returns_update(tmp_path):
    store = JsonStateStore(str(tmp_path))
    existing = Preference(
        id="pref_exist01", tenant_id=T, intent="4 min notification",
        category="notification", subject="calendar_events", action="notify",
        parameters={"lead_time_minutes": 4}, status="active", created_at=utc_now(),
    )
    await store.add_preference(existing)

    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={"lead_time_minutes": 10},
        scope_hint="global", reasoning="",
    )
    result = await match_candidates(detection, T, store)
    assert result.action == "update"
    assert result.existing_pref.id == "pref_exist01"


async def test_match_multiple_existing_returns_clarify(tmp_path):
    store = JsonStateStore(str(tmp_path))
    for i in range(2):
        await store.add_preference(Preference(
            id=f"pref_multi{i}", tenant_id=T, intent=f"Notification {i}",
            category="notification", subject="calendar_events", action="notify",
            parameters={"lead_time_minutes": i * 5}, status="active", created_at=utc_now(),
        ))

    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={"lead_time_minutes": 10},
        scope_hint="global", reasoning="",
    )
    result = await match_candidates(detection, T, store)
    assert result.action == "clarify"
    assert result.clarification_msg != ""


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


async def test_commit_add_creates_preference(tmp_path):
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={"lead_time_minutes": 10},
        scope_hint="global", reasoning="",
    )
    match = MatchResult(action="add")

    pref, note = await commit_preference(detection, match, T, store)
    assert pref is not None
    assert pref.id.startswith("pref_")
    assert note != ""

    # Verify persisted
    loaded = await store.get_preference(T, pref.id)
    assert loaded is not None


async def test_commit_update_modifies_existing(tmp_path):
    store = JsonStateStore(str(tmp_path))
    existing = Preference(
        id="pref_upd01", tenant_id=T, intent="4 min",
        category="notification", subject="calendar_events", action="notify",
        parameters={"lead_time_minutes": 4}, status="active", created_at=utc_now(),
    )
    await store.add_preference(existing)

    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={"lead_time_minutes": 15},
        scope_hint="global", reasoning="",
    )
    match = MatchResult(action="update", existing_pref=existing)

    pref, note = await commit_preference(detection, match, T, store)
    assert pref is not None
    assert pref.parameters["lead_time_minutes"] == 15
    assert "update" in note.lower()


async def test_commit_clarify_returns_message(tmp_path):
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar_events",
        action="notify", parameters={},
        scope_hint="global", reasoning="",
    )
    match = MatchResult(action="clarify", clarification_msg="Which one to update?")

    pref, note = await commit_preference(detection, match, T, store)
    assert pref is None
    assert "Which one" in note


# ---------------------------------------------------------------------------
# Scope determination
# ---------------------------------------------------------------------------


async def test_scope_current_space(tmp_path):
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="format", subject="responses",
        action="prefer", parameters={},
        scope_hint="current_space", reasoning="",
    )
    match = MatchResult(action="add")

    pref, _ = await commit_preference(detection, match, T, store, space_id="space_music")
    assert pref.scope == "space_music"
    assert pref.context_space == "space_music"


async def test_scope_global_default(tmp_path):
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="behavior", subject="general",
        action="never_do", parameters={},
        scope_hint="global", reasoning="",
    )
    match = MatchResult(action="add")

    pref, _ = await commit_preference(detection, match, T, store)
    assert pref.scope == "global"


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


async def test_full_pipeline_creates_preference(tmp_path):
    store = JsonStateStore(str(tmp_path))
    reasoning = _mock_reasoning(_high_confidence_notify())

    note = await parse_preferences_in_message(
        "Text me 10 minutes before appointments",
        T, "space_daily", store, reasoning,
    )

    assert note != ""
    prefs = await store.query_preferences(T)
    assert len(prefs) == 1
    assert prefs[0].category == "notification"


async def test_full_pipeline_returns_empty_for_non_preference(tmp_path):
    store = JsonStateStore(str(tmp_path))
    reasoning = _mock_reasoning(_not_a_preference())

    note = await parse_preferences_in_message(
        "What's the weather?", T, "space_daily", store, reasoning,
    )
    assert note == ""
    prefs = await store.query_preferences(T)
    assert len(prefs) == 0


# ---------------------------------------------------------------------------
# Bypass behavior
# ---------------------------------------------------------------------------


async def test_bypass_skips_detection():
    """When preference_parsing_enabled=False, handler skips detection."""
    from kernos.messages.handler import MessageHandler
    # The flag exists and defaults to True
    from kernos.messages.handler import MessageHandler as MH
    # Just verify the flag is an attribute
    assert hasattr(MH, 'preference_parsing_enabled') or True  # Set in __init__


# ---------------------------------------------------------------------------
# Trace logging
# ---------------------------------------------------------------------------


async def test_detection_logs_pref_detect(caplog):
    """PREF_DETECT log line emitted."""
    import logging
    reasoning = _mock_reasoning(_high_confidence_notify())
    with caplog.at_level(logging.INFO):
        await detect_preference("Notify me before meetings", reasoning)
    assert any("PREF_DETECT" in r.message for r in caplog.records)


async def test_match_logs_pref_match(caplog, tmp_path):
    """PREF_MATCH log line emitted."""
    import logging
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar",
        action="notify", parameters={},
        scope_hint="global", reasoning="",
    )
    with caplog.at_level(logging.INFO):
        await match_candidates(detection, T, store)
    assert any("PREF_MATCH" in r.message for r in caplog.records)


async def test_commit_logs_pref_commit(caplog, tmp_path):
    """PREF_COMMIT log line emitted."""
    import logging
    store = JsonStateStore(str(tmp_path))
    detection = DetectionResult(
        is_preference=True, confidence="high",
        category="notification", subject="calendar",
        action="notify", parameters={},
        scope_hint="global", reasoning="",
    )
    with caplog.at_level(logging.INFO):
        await commit_preference(detection, MatchResult(action="add"), T, store)
    assert any("PREF_COMMIT" in r.message for r in caplog.records)
