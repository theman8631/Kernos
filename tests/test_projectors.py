"""Tests for SPEC-1B7: Memory Projectors (Tier 1, Tier 2, coordinator, name ask)."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kernos.kernel.projectors.rules import Tier1Result, tier1_extract
from kernos.kernel.projectors.llm_extractor import (
    run_tier2_extraction,
    _write_entry,
    _apply_correction,
)
from kernos.kernel.projectors.coordinator import run_projectors
from kernos.kernel.soul import Soul
from kernos.kernel.state import KnowledgeEntry, StateStore, _content_hash
from kernos.kernel.events import EventStream
from kernos.messages.handler import _maybe_append_name_ask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soul(**kwargs) -> Soul:
    return Soul(tenant_id="t1", **kwargs)


def _mock_state() -> AsyncMock:
    state = AsyncMock(spec=StateStore)
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []
    state.save_soul.return_value = None
    state.save_knowledge_entry.return_value = None
    return state


def _mock_events() -> AsyncMock:
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    return events


def _knowledge_entry(tenant_id: str, subject: str, content: str,
                     confidence: str = "stated", active: bool = True) -> KnowledgeEntry:
    h = _content_hash(tenant_id, subject, content)
    return KnowledgeEntry(
        id=f"know_test_{h}",
        tenant_id=tenant_id,
        category="fact",
        subject=subject,
        content=content,
        confidence=confidence,
        source_event_id="",
        source_description="test",
        created_at="2026-01-01T00:00:00+00:00",
        last_referenced="2026-01-01T00:00:00+00:00",
        tags=[],
        active=active,
        durability="permanent",
        content_hash=h,
    )


# ---------------------------------------------------------------------------
# Tier 1 — name extraction
# ---------------------------------------------------------------------------


def test_tier1_extracts_my_name_is():
    result = tier1_extract("my name is Alice")
    assert result.user_name == "Alice"


def test_tier1_extracts_call_me():
    result = tier1_extract("You can call me Bob")
    assert result.user_name == "Bob"


def test_tier1_extracts_i_go_by():
    result = tier1_extract("I go by Charlie")
    assert result.user_name == "Charlie"


def test_tier1_extracts_they_call_me():
    result = tier1_extract("They call me Doc")
    assert result.user_name == "Doc"


def test_tier1_extracts_everyone_calls_me():
    result = tier1_extract("Everyone calls me Sam")
    assert result.user_name == "Sam"


def test_tier1_extracts_im_with_comma():
    result = tier1_extract("I'm Dana, how are you?")
    assert result.user_name == "Dana"


def test_tier1_extracts_its_at_start():
    result = tier1_extract("It's Jordan here")
    assert result.user_name == "Jordan"


def test_tier1_capitalizes_name():
    result = tier1_extract("my name is alice")
    assert result.user_name == "Alice"


def test_tier1_false_positive_fine():
    result = tier1_extract("I'm fine, thanks")
    assert result.user_name == ""


def test_tier1_false_positive_good():
    result = tier1_extract("I'm good")
    assert result.user_name == ""


def test_tier1_false_positive_here():
    result = tier1_extract("I'm here now")
    assert result.user_name == ""


def test_tier1_false_positive_back():
    result = tier1_extract("I'm back!")
    assert result.user_name == ""


def test_tier1_false_positive_ready():
    result = tier1_extract("I'm ready")
    assert result.user_name == ""


def test_tier1_false_positive_new():
    result = tier1_extract("I'm new here")
    assert result.user_name == ""


def test_tier1_extracts_name_even_if_already_set():
    # Name is always authoritative — "my name is Alice" overwrites previous value
    result = tier1_extract("my name is Alice", current_name="Bob")
    assert result.user_name == "Alice"


def test_tier1_rejects_single_char_name():
    result = tier1_extract("call me X")
    assert result.user_name == ""


def test_tier1_no_name_in_message():
    result = tier1_extract("Just checking in, how's everything?")
    assert result.user_name == ""


# ---------------------------------------------------------------------------
# Tier 1 — style extraction
# ---------------------------------------------------------------------------


def test_tier1_direct_style_be_direct():
    result = tier1_extract("Please be direct with me")
    assert result.communication_style == "direct"


def test_tier1_direct_style_be_blunt():
    result = tier1_extract("just be blunt with me")
    assert result.communication_style == "direct"


def test_tier1_direct_style_straight():
    result = tier1_extract("be straight with me about it")
    assert result.communication_style == "direct"


def test_tier1_casual_keep_it_casual():
    result = tier1_extract("keep it casual please")
    assert result.communication_style == "casual"


def test_tier1_casual_no_need_to_be_formal():
    result = tier1_extract("no need to be formal with me")
    assert result.communication_style == "casual"


def test_tier1_casual_dont_sugarcoat():
    result = tier1_extract("Don't sugarcoat things")
    assert result.communication_style == "casual"


def test_tier1_formal_keep_it_professional():
    result = tier1_extract("Keep it professional please")
    assert result.communication_style == "formal"


def test_tier1_formal_be_formal():
    result = tier1_extract("be formal with me")
    assert result.communication_style == "formal"


def test_tier1_skips_style_if_already_set():
    result = tier1_extract("be direct with me", current_style="formal")
    assert result.communication_style == ""


def test_tier1_no_style_in_message():
    result = tier1_extract("What's on my calendar today?")
    assert result.communication_style == ""


def test_tier1_result_defaults_empty():
    result = Tier1Result()
    assert result.user_name == ""
    assert result.communication_style == ""


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------


def test_content_hash_deterministic():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_case_insensitive():
    h1 = _content_hash("t1", "user", "Runs A Bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_subject_case_insensitive():
    h1 = _content_hash("t1", "User", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_different_content():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a restaurant")
    assert h1 != h2


def test_content_hash_tenant_isolated():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t2", "user", "runs a bakery")
    assert h1 != h2


def test_content_hash_length():
    h = _content_hash("t1", "user", "content")
    assert len(h) == 16


# ---------------------------------------------------------------------------
# _parse_extraction_response — REMOVED in SPEC-2.0
# Structured output (output_schema) guarantees valid JSON from the API.
# Code fence stripping is no longer needed. Tests moved to test_schema_foundation.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _write_entry — deduplication and confidence precedence
# ---------------------------------------------------------------------------


async def test_write_entry_writes_new():
    state = _mock_state()
    events = _mock_events()
    wrote = await _write_entry(
        state=state, events=events, tenant_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 1
    state.save_knowledge_entry.assert_called_once()
    saved = state.save_knowledge_entry.call_args[0][0]
    assert saved.content == "runs a bakery"
    assert saved.active is True


async def test_write_entry_skips_duplicate_hash():
    state = _mock_state()
    events = _mock_events()
    h = _content_hash("t1", "user", "runs a bakery")
    wrote = await _write_entry(
        state=state, events=events, tenant_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes={h},
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 0
    state.save_knowledge_entry.assert_not_called()


async def test_write_entry_inferred_discarded_when_stated_exists():
    state = _mock_state()
    events = _mock_events()
    existing = _knowledge_entry("t1", "user", "runs a bakery", confidence="stated")
    state.query_knowledge.return_value = [existing]

    wrote = await _write_entry(
        state=state, events=events, tenant_id="t1",
        category="fact", subject="user", content="different content",
        confidence="inferred",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 0
    state.save_knowledge_entry.assert_not_called()


async def test_write_entry_stated_marks_inferred_inactive():
    state = _mock_state()
    events = _mock_events()
    inferred_entry = _knowledge_entry("t1", "user", "probably a developer", confidence="inferred")
    state.query_knowledge.return_value = [inferred_entry]

    await _write_entry(
        state=state, events=events, tenant_id="t1",
        category="fact", subject="user", content="definitely a developer",
        confidence="stated",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    # First call marks old entry inactive, second writes new entry
    assert state.save_knowledge_entry.call_count == 2
    first_saved = state.save_knowledge_entry.call_args_list[0][0][0]
    assert first_saved.active is False
    assert first_saved.id == inferred_entry.id


async def test_write_entry_hash_added_to_existing_set():
    state = _mock_state()
    events = _mock_events()
    existing = set()
    await _write_entry(
        state=state, events=events, tenant_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes=existing,
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    h = _content_hash("t1", "user", "runs a bakery")
    assert h in existing


# ---------------------------------------------------------------------------
# _apply_correction
# ---------------------------------------------------------------------------


async def test_apply_correction_marks_old_inactive():
    state = _mock_state()
    events = _mock_events()
    old_entry = _knowledge_entry("t1", "user", "call me Alice")
    state.query_knowledge.return_value = [old_entry]
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, tenant_id="t1",
        field="user_name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    calls = state.save_knowledge_entry.call_args_list
    # First call: mark old inactive
    assert calls[0][0][0].active is False
    assert calls[0][0][0].id == old_entry.id
    # Second call: new entry
    new_entry = calls[1][0][0]
    assert new_entry.content == "JT"
    assert new_entry.supersedes == old_entry.id
    assert new_entry.active is True


async def test_apply_correction_updates_soul_name():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, tenant_id="t1",
        field="user_name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    assert soul.user_name == "JT"
    state.save_soul.assert_called_once_with(soul)


async def test_apply_correction_updates_soul_name_dot_notation():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, tenant_id="t1",
        field="user.name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    assert soul.user_name == "JT"
    state.save_soul.assert_called_once_with(soul)


async def test_apply_correction_no_old_entry_still_creates_new():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul()

    await _apply_correction(
        state=state, events=events, soul=soul, tenant_id="t1",
        field="occupation", old_value="teacher", new_value="nurse",
        now="2026-01-01T00:00:00+00:00",
    )

    # Only one call — the new entry (no old to mark inactive)
    state.save_knowledge_entry.assert_called_once()
    new_entry = state.save_knowledge_entry.call_args[0][0]
    assert new_entry.supersedes == ""
    assert new_entry.content == "nurse"


async def test_apply_correction_emits_event():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul()

    await _apply_correction(
        state=state, events=events, soul=soul, tenant_id="t1",
        field="name", old_value="old", new_value="new",
        now="2026-01-01T00:00:00+00:00",
    )

    events.emit.assert_called_once()
    event = events.emit.call_args[0][0]
    assert "correction" in event.payload.get("type", "")


# ---------------------------------------------------------------------------
# run_tier2_extraction
# ---------------------------------------------------------------------------


async def test_tier2_writes_facts():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    soul = _soul(user_context="")
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=soul, state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    state.save_knowledge_entry.assert_called()
    saved = state.save_knowledge_entry.call_args[0][0]
    assert saved.content == "runs a bakery"


async def test_tier2_appends_user_fact_to_soul_context():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    soul = _soul(user_context="")
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=soul, state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    assert "runs a bakery" in soul.user_context
    state.save_soul.assert_called()


async def test_tier2_skips_empty_turns():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock()

    await run_tier2_extraction(
        recent_turns=[],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    reasoning.complete_simple.assert_not_called()


async def test_tier2_handles_malformed_json_gracefully():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="not json at all")

    # Should not raise
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )
    state.save_knowledge_entry.assert_not_called()


async def test_tier2_handles_reasoning_error_gracefully():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(side_effect=RuntimeError("API down"))

    # Should not raise
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )


async def test_tier2_emits_knowledge_extracted_on_writes():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "is a nurse",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I'm a nurse"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    events.emit.assert_called()
    event = events.emit.call_args[0][0]
    assert event.type == "knowledge.extracted"
    assert event.payload["entries_written"] == 1


async def test_tier2_no_emit_when_nothing_written():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [], "facts": [], "preferences": [], "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi there"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    events.emit.assert_not_called()


async def test_tier2_deduplicates_via_hash():
    state = _mock_state()
    events = _mock_events()
    # Pre-populate hash so write is skipped
    h = _content_hash("t1", "user", "runs a bakery")
    state.get_knowledge_hashes.return_value = {h}
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    state.save_knowledge_entry.assert_not_called()


async def test_tier2_writes_teacher_fact():
    """Structured output (no code fences) writes knowledge correctly."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    data = {
        "reasoning": "User is a teacher",
        "entities": [],
        "facts": [{"subject": "user", "content": "is a teacher",
                   "confidence": "stated", "lifecycle_archetype": "structural",
                   "foresight_signal": "", "foresight_expires": "", "salience": "0.5"}],
        "preferences": [],
        "corrections": [],
    }
    reasoning.complete_simple = AsyncMock(return_value=json.dumps(data))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I'm a teacher"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, tenant_id="t1",
    )

    state.save_knowledge_entry.assert_called()


# ---------------------------------------------------------------------------
# run_projectors (coordinator)
# ---------------------------------------------------------------------------


async def test_coordinator_tier1_updates_name():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value='{"entities":[],"facts":[],"preferences":[],"corrections":[]}')
    soul = _soul(user_name="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    assert soul.user_name == "Alice"
    state.save_soul.assert_called_with(soul)


async def test_coordinator_tier1_emits_event_on_update():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(user_name="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    events.emit.assert_called()
    event = events.emit.call_args[0][0]
    assert event.type == "knowledge.extracted"
    assert "user_name" in event.payload["fields_updated"]


async def test_coordinator_tier1_overwrites_existing_name():
    # Stated name is always authoritative — coordinator updates even if already set
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(user_name="Bob")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    assert soul.user_name == "Alice"
    state.save_soul.assert_called_with(soul)


async def test_coordinator_tier1_no_save_when_name_unchanged():
    # Same name → no unnecessary write
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(user_name="Alice")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    assert soul.user_name == "Alice"
    state.save_soul.assert_not_called()


async def test_coordinator_tier1_updates_style():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(communication_style="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="Please be direct with me",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    assert soul.communication_style == "direct"


async def test_coordinator_tier1_no_update_on_no_match():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul()

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="What's the weather like?",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    state.save_soul.assert_not_called()
    events.emit.assert_not_called()


async def test_coordinator_schedules_tier2():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul()

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task") as mock_create_task:
        await run_projectors(
            user_message="Hello",
            recent_turns=[{"role": "user", "content": "Hello"}],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, tenant_id="t1",
        )

    mock_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _maybe_append_name_ask
# ---------------------------------------------------------------------------


def test_name_ask_appended_on_first_interaction_no_name():
    soul = Soul(tenant_id="t1", interaction_count=0, user_name="")
    result = _maybe_append_name_ask("Hello! Nice to meet you.", soul)
    assert result.startswith("Hello! Nice to meet you.")
    assert "what should I call you" in result.lower() or "call you" in result.lower()


def test_name_ask_not_appended_if_name_set():
    soul = Soul(tenant_id="t1", interaction_count=0, user_name="Alice")
    result = _maybe_append_name_ask("Hello!", soul)
    assert result == "Hello!"


def test_name_ask_not_appended_if_not_first_interaction():
    soul = Soul(tenant_id="t1", interaction_count=3, user_name="")
    result = _maybe_append_name_ask("Hello!", soul)
    assert result == "Hello!"


def test_name_ask_not_appended_if_response_already_asks_name():
    soul = Soul(tenant_id="t1", interaction_count=0, user_name="")
    response = "Hey! What's your name?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response


def test_name_ask_not_appended_if_response_says_call_you():
    soul = Soul(tenant_id="t1", interaction_count=0, user_name="")
    response = "Hi there! What should I call you?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response


def test_name_ask_not_appended_if_response_asks_who_am_i_talking():
    soul = Soul(tenant_id="t1", interaction_count=0, user_name="")
    response = "Hey! Who am I talking to today?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response
