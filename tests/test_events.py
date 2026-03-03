"""Tests for kernos.kernel.events — Event, JsonEventStream, helpers."""
import pytest

from kernos.kernel.events import (
    Event,
    JsonEventStream,
    emit_event,
    estimate_cost,
    generate_event_id,
)


# --- generate_event_id ---


def test_generate_event_id_unique():
    ids = {generate_event_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_generate_event_id_format():
    eid = generate_event_id()
    assert eid.startswith("evt_")
    parts = eid.split("_")
    assert len(parts) == 3
    assert len(parts[2]) == 4  # 4 random hex chars


# --- Event dataclass ---


def test_event_fields_all_present():
    e = Event(
        id="evt_123_abcd",
        type="message.received",
        tenant_id="sms:+15555550100",
        timestamp="2026-03-03T00:00:00+00:00",
        source="handler",
        payload={"content": "Hello"},
        metadata={"conversation_id": "+15555550100"},
    )
    assert e.id == "evt_123_abcd"
    assert e.type == "message.received"
    assert e.tenant_id == "sms:+15555550100"
    assert e.source == "handler"
    assert e.payload == {"content": "Hello"}
    assert e.metadata == {"conversation_id": "+15555550100"}


def test_event_is_immutable():
    e = Event(
        id="evt_1",
        type="test",
        tenant_id="t1",
        timestamp="2026-03-03T00:00:00+00:00",
        source="test",
        payload={},
        metadata={},
    )
    with pytest.raises((AttributeError, TypeError)):
        e.type = "modified"  # type: ignore[misc]


# --- estimate_cost ---


def test_estimate_cost_known_model():
    # claude-sonnet-4-6: $3.00/M input, $15.00/M output
    cost = estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert abs(cost - 18.00) < 0.001


def test_estimate_cost_per_token():
    cost = estimate_cost("claude-sonnet-4-6", 100, 50)
    expected = (100 * 3.00 / 1_000_000) + (50 * 15.00 / 1_000_000)
    assert abs(cost - expected) < 1e-9


def test_estimate_cost_unknown_model():
    cost = estimate_cost("gpt-9999", 1000, 1000)
    assert cost == 0.0


def test_estimate_cost_zero_tokens():
    cost = estimate_cost("claude-sonnet-4-6", 0, 0)
    assert cost == 0.0


# --- JsonEventStream ---


async def test_jsonstream_emit_and_query(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "tenant1", "handler", {"content": "Hi"})
    events = await stream.query("tenant1")
    assert len(events) == 1
    assert events[0].type == "message.received"
    assert events[0].tenant_id == "tenant1"
    assert events[0].payload["content"] == "Hi"


async def test_jsonstream_tenant_isolation(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "tenant_a", "handler", {"x": 1})
    await emit_event(stream, "message.received", "tenant_b", "handler", {"x": 2})

    a_events = await stream.query("tenant_a")
    b_events = await stream.query("tenant_b")

    assert len(a_events) == 1
    assert len(b_events) == 1
    assert a_events[0].payload["x"] == 1
    assert b_events[0].payload["x"] == 2


async def test_jsonstream_query_by_type(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "t1", "handler", {})
    await emit_event(stream, "reasoning.response", "t1", "handler", {})
    await emit_event(stream, "message.sent", "t1", "handler", {})

    msg_events = await stream.query("t1", event_types=["message.received"])
    assert len(msg_events) == 1
    assert msg_events[0].type == "message.received"


async def test_jsonstream_query_multiple_types(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "t1", "handler", {})
    await emit_event(stream, "reasoning.response", "t1", "handler", {})
    await emit_event(stream, "message.sent", "t1", "handler", {})

    filtered = await stream.query(
        "t1", event_types=["message.received", "message.sent"]
    )
    assert len(filtered) == 2


async def test_jsonstream_query_empty_tenant(tmp_path):
    stream = JsonEventStream(tmp_path)
    events = await stream.query("nonexistent_tenant")
    assert events == []


async def test_jsonstream_count(tmp_path):
    stream = JsonEventStream(tmp_path)
    for i in range(5):
        await emit_event(stream, "message.received", "t1", "handler", {"i": i})

    total = await stream.count("t1")
    assert total == 5

    by_type = await stream.count("t1", event_types=["message.received"])
    assert by_type == 5

    other = await stream.count("t1", event_types=["reasoning.response"])
    assert other == 0


async def test_jsonstream_query_limit(tmp_path):
    stream = JsonEventStream(tmp_path)
    for i in range(20):
        await emit_event(stream, "test.event", "t1", "handler", {"i": i})

    limited = await stream.query("t1", limit=5)
    assert len(limited) == 5


async def test_emit_event_helper_returns_event(tmp_path):
    stream = JsonEventStream(tmp_path)
    event = await emit_event(stream, "test.type", "t1", "source", {"key": "value"})
    assert event.id.startswith("evt_")
    assert event.type == "test.type"
    assert event.tenant_id == "t1"
    assert event.source == "source"
    assert event.payload == {"key": "value"}


async def test_emit_event_persists(tmp_path):
    stream = JsonEventStream(tmp_path)
    emitted = await emit_event(stream, "test.type", "t1", "src", {})
    queried = await stream.query("t1")
    assert len(queried) == 1
    assert queried[0].id == emitted.id


async def test_jsonstream_tenant_id_with_colon(tmp_path):
    """Tenant IDs like 'sms:+15555550100' map to valid filesystem paths."""
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "sms:+15555550100", "handler", {})
    events = await stream.query("sms:+15555550100")
    assert len(events) == 1


async def test_jsonstream_metadata_preserved(tmp_path):
    stream = JsonEventStream(tmp_path)
    meta = {"conversation_id": "conv_123", "platform": "sms"}
    event = await emit_event(stream, "msg.received", "t1", "handler", {}, metadata=meta)
    queried = await stream.query("t1")
    assert queried[0].metadata == meta


async def test_emit_event_no_metadata_defaults_empty(tmp_path):
    stream = JsonEventStream(tmp_path)
    event = await emit_event(stream, "test", "t1", "src", {"k": "v"})
    assert event.metadata == {}
