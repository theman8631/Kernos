"""Tests for event emission in MessageHandler.process()."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from kernos.capability.client import MCPClientManager
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, JsonEventStream
from kernos.kernel.state import StateStore, TenantProfile
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import MessageHandler
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, TenantStore


# ---------------------------------------------------------------------------
# Local test helpers (mirrors test_handler.py, kept self-contained)
# ---------------------------------------------------------------------------


def _make_message(content: str = "Hello", platform: str = "sms") -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform=platform,
        platform_capabilities=["text", "mms"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        tenant_id="+15555550100",
    )


def _mock_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage.input_tokens = 10
    response.usage.output_tokens = 20
    return response


def _mock_tool_use_response(
    tool_name: str, tool_id: str, tool_input: dict
) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.id = tool_id
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage.input_tokens = 15
    response.usage.output_tokens = 5
    return response


def _make_mock_handler(tools: list[dict] | None = None):
    """Handler with mock EventStream and StateStore (no disk I/O)."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = tools or []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=TenantStore)
    tenants.get_or_create.return_value = {
        "tenant_id": "sms:+15555550100",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None

    state = AsyncMock(spec=StateStore)
    state.get_tenant_profile.return_value = TenantProfile(
        tenant_id="sms:+15555550100",
        status="active",
        created_at="2026-03-01T00:00:00Z",
    )
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None

    mock_client = MagicMock()
    with patch("kernos.messages.handler.anthropic.Anthropic", return_value=mock_client):
        handler = MessageHandler(mcp, conversations, tenants, audit, events, state)
    return handler, mock_client


def _make_real_handler(tmp_path):
    """Handler with real JsonStateStore and JsonEventStream (writes to tmp_path)."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=TenantStore)
    tenants.get_or_create.return_value = {
        "tenant_id": "sms:+15555550100",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    events = JsonEventStream(tmp_path)
    state = JsonStateStore(tmp_path)

    mock_client = MagicMock()
    with patch("kernos.messages.handler.anthropic.Anthropic", return_value=mock_client):
        handler = MessageHandler(mcp, conversations, tenants, audit, events, state)
    return handler, mock_client, events, state


def _emitted_types(handler: MessageHandler) -> list[str]:
    """Return event types emitted via the mock EventStream."""
    return [c.args[0].type for c in handler.events.emit.call_args_list]


# ---------------------------------------------------------------------------
# Basic flow event emission
# ---------------------------------------------------------------------------


async def test_handler_emits_message_received():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    assert EventType.MESSAGE_RECEIVED in _emitted_types(handler)


async def test_handler_emits_reasoning_request_and_response():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    types = _emitted_types(handler)
    assert EventType.REASONING_REQUEST in types
    assert EventType.REASONING_RESPONSE in types


async def test_handler_emits_message_sent():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    assert EventType.MESSAGE_SENT in _emitted_types(handler)


async def test_reasoning_response_has_token_counts():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    rr = next(e for e in emitted if e.type == EventType.REASONING_RESPONSE)
    assert rr.payload["input_tokens"] == 10
    assert rr.payload["output_tokens"] == 20
    assert "estimated_cost_usd" in rr.payload
    assert "duration_ms" in rr.payload


async def test_message_received_has_content_and_platform():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message("Hello there"))

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    mr = next(e for e in emitted if e.type == EventType.MESSAGE_RECEIVED)
    assert mr.payload["content"] == "Hello there"
    assert mr.payload["platform"] == "sms"


async def test_message_sent_has_content():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("I'm good!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    ms = next(e for e in emitted if e.type == EventType.MESSAGE_SENT)
    assert ms.payload["content"] == "I'm good!"


async def test_all_events_have_tenant_id():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    for event in emitted:
        assert event.tenant_id == "sms:+15555550100"


# ---------------------------------------------------------------------------
# Tool-use events
# ---------------------------------------------------------------------------


async def test_handler_emits_tool_called_and_result():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_text_response("You have a meeting at 10am."),
    ]

    await handler.process(_make_message("What's on my schedule?"))

    types = _emitted_types(handler)
    assert EventType.TOOL_CALLED in types
    assert EventType.TOOL_RESULT in types


async def test_tool_called_event_has_tool_name_and_input():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Event data")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_text_response("Done"),
    ]

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tc = next(e for e in emitted if e.type == EventType.TOOL_CALLED)
    assert tc.payload["tool_name"] == "list_events"
    assert tc.payload["tool_input"] == {"date": "2026-03-01"}


async def test_tool_result_success_flag_on_success():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {}),
        _mock_text_response("Done"),
    ]

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tr = next(e for e in emitted if e.type == EventType.TOOL_RESULT)
    assert tr.payload["success"] is True
    assert tr.payload["error"] is None


async def test_tool_result_error_flag_on_tool_error():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Tool error: connection failed")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {}),
        _mock_text_response("I had trouble with that."),
    ]

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tr = next(e for e in emitted if e.type == EventType.TOOL_RESULT)
    assert tr.payload["success"] is False
    assert tr.payload["error"] is not None


# ---------------------------------------------------------------------------
# Error events
# ---------------------------------------------------------------------------


async def test_handler_emits_handler_error_on_timeout():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(
        request=MagicMock()
    )

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_emits_handler_error_on_connection_error():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.side_effect = anthropic.APIConnectionError(
        request=MagicMock()
    )

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_emits_handler_error_on_unexpected():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.side_effect = RuntimeError("boom")

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_error_event_has_error_type():
    handler, mock_client = _make_mock_handler()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(
        request=MagicMock()
    )

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    he = next(e for e in emitted if e.type == EventType.HANDLER_ERROR)
    assert he.payload["error_type"] == "APITimeoutError"
    assert "stage" in he.payload


# ---------------------------------------------------------------------------
# Tenant provisioning — real StateStore + EventStream (integration)
# ---------------------------------------------------------------------------


async def test_new_tenant_gets_profile_and_7_contracts(tmp_path):
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    await handler.process(_make_message())

    tenant_id = "sms:+15555550100"
    profile = await state.get_tenant_profile(tenant_id)
    assert profile is not None
    assert profile.status == "active"

    rules = await state.get_contract_rules(tenant_id)
    assert len(rules) == 7


async def test_new_tenant_emits_provisioned_event(tmp_path):
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    await handler.process(_make_message())

    tenant_id = "sms:+15555550100"
    emitted = await events.query(tenant_id, limit=100)
    event_types = [e.type for e in emitted]
    assert EventType.TENANT_PROVISIONED in event_types


async def test_existing_tenant_skips_provisioning(tmp_path):
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    # First call provisions the tenant
    await handler.process(_make_message())
    rules_after_first = await state.get_contract_rules("sms:+15555550100")

    # Second call must not double-seed contracts
    await handler.process(_make_message())
    rules_after_second = await state.get_contract_rules("sms:+15555550100")

    assert len(rules_after_second) == len(rules_after_first)


async def test_handler_updates_conversation_summary(tmp_path):
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    await handler.process(_make_message())

    tenant_id = "sms:+15555550100"
    summary = await state.get_conversation_summary(tenant_id, "+15555550100")
    assert summary is not None
    assert summary.message_count >= 1
    assert summary.platform == "sms"


async def test_full_event_sequence_written_to_disk(tmp_path):
    """End-to-end: events emitted in process() are queryable from disk."""
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    tenant_id = "sms:+15555550100"
    all_events = await events.query(tenant_id, limit=100)
    types = [e.type for e in all_events]

    assert EventType.MESSAGE_RECEIVED in types
    assert EventType.REASONING_REQUEST in types
    assert EventType.REASONING_RESPONSE in types
    assert EventType.MESSAGE_SENT in types


async def test_disk_events_all_have_correct_tenant_id(tmp_path):
    handler, mock_client, events, state = _make_real_handler(tmp_path)
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    await handler.process(_make_message())

    tenant_id = "sms:+15555550100"
    all_events = await events.query(tenant_id, limit=100)
    assert len(all_events) > 0
    for event in all_events:
        assert event.tenant_id == tenant_id
