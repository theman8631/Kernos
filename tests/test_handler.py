from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from kernos.messages.handler import MessageHandler, _build_system_prompt
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.capability.client import MCPClientManager
from kernos.persistence import AuditStore, ConversationStore, TenantStore
from kernos.kernel.events import EventStream
from kernos.kernel.state import StateStore, TenantProfile


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


def _make_handler(tools: list[dict] | None = None) -> tuple[MessageHandler, MagicMock]:
    """Return a (handler, mock_anthropic_client) with mock MCP, stores, events, and state."""
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
    # Return an existing profile so provisioning is skipped in most tests
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


# --- Happy path ---


async def test_process_returns_string():
    handler, mock_client = _make_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hi!")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert result == "Hi!"


# --- Error paths: each must return a friendly string, never raise ---


async def test_timeout_returns_friendly_string():
    handler, mock_client = _make_handler()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(
        request=MagicMock()
    )

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_connection_error_returns_friendly_string():
    handler, mock_client = _make_handler()
    mock_client.messages.create.side_effect = anthropic.APIConnectionError(
        request=MagicMock()
    )

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_rate_limit_returns_friendly_string():
    handler, mock_client = _make_handler()
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    mock_client.messages.create.side_effect = anthropic.RateLimitError(
        message="rate limited", response=mock_response, body=None
    )

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "overloaded" in result.lower() or "minute" in result.lower()


async def test_api_status_error_returns_friendly_string():
    handler, mock_client = _make_handler()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_client.messages.create.side_effect = anthropic.APIStatusError(
        message="Internal Server Error", response=mock_response, body=None
    )

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_unexpected_error_returns_friendly_string():
    handler, mock_client = _make_handler()
    mock_client.messages.create.side_effect = RuntimeError("something broke")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert len(result) > 0


# --- System prompt ---


def test_system_prompt_includes_platform():
    base = dict(
        content="Hi",
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform_capabilities=["text"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        tenant_id="+15555550100",
    )

    discord_msg = NormalizedMessage(**base, platform="discord")
    assert "Discord" in _build_system_prompt(discord_msg)
    assert "SMS" not in _build_system_prompt(discord_msg)

    sms_msg = NormalizedMessage(**base, platform="sms")
    assert "SMS" in _build_system_prompt(sms_msg)
    assert "Discord" not in _build_system_prompt(sms_msg)


def test_system_prompt_no_tools_is_conversation_only():
    msg = _make_message()
    prompt = _build_system_prompt(msg, tools=[])
    assert "Google Calendar:" not in prompt
    assert "conversation" in prompt.lower()


def test_system_prompt_with_calendar_tools_claims_calendar():
    msg = _make_message()
    tools = [{"name": "list_events", "description": "List calendar events", "input_schema": {}}]
    prompt = _build_system_prompt(msg, tools=tools)
    assert "calendar" in prompt.lower() or "Calendar" in prompt


def test_system_prompt_does_not_claim_cannot_remember():
    msg = _make_message()
    prompt_no_tools = _build_system_prompt(msg, tools=[])
    prompt_with_tools = _build_system_prompt(
        msg,
        tools=[{"name": "list_events", "description": "List events", "input_schema": {}}],
    )
    assert "cannot remember previous conversations" not in prompt_no_tools
    assert "cannot remember previous conversations" not in prompt_with_tools


# --- MCP tool-use loop ---


async def test_handler_with_tool_use_brokers_call_and_returns_text():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_text_response("You have a meeting at 10am."),
    ]

    result = await handler.process(_make_message("What's on my schedule?"))
    assert "10am" in result
    handler.mcp.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_handler_safety_valve_returns_graceful_message():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="some result")

    mock_client.messages.create.return_value = _mock_tool_use_response(
        "list_events", "tu_001", {}
    )

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert "trouble" in result.lower() or "simpler" in result.lower()


async def test_handler_tool_failure_results_in_graceful_response():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Calendar tool error: connection failed")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {}),
        _mock_text_response("I had trouble checking your calendar."),
    ]

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert len(result) > 0


async def test_handler_no_tools_works_identically_to_1a2():
    handler, mock_client = _make_handler(tools=[])
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    result = await handler.process(_make_message())
    assert result == "Hello!"
    assert mock_client.messages.create.call_count == 1


# --- Persistence (AC19) ---


async def test_handler_stores_user_and_assistant_messages():
    handler, mock_client = _make_handler()
    mock_client.messages.create.return_value = _mock_text_response("I'm good, thanks!")

    await handler.process(_make_message("How are you?"))

    assert handler.conversations.append.await_count == 2
    calls = handler.conversations.append.await_args_list

    user_entry = calls[0][0][2]
    assert user_entry["role"] == "user"
    assert user_entry["content"] == "How are you?"
    assert "tenant_id" in user_entry

    assistant_entry = calls[1][0][2]
    assert assistant_entry["role"] == "assistant"
    assert assistant_entry["content"] == "I'm good, thanks!"
    assert "tenant_id" in assistant_entry


async def test_handler_tool_calls_go_to_audit_not_conversation():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_client = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_client.messages.create.side_effect = [
        _mock_tool_use_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_text_response("You have a meeting at 10am."),
    ]

    await handler.process(_make_message("What's on my schedule?"))

    assert handler.conversations.append.await_count == 2
    assert handler.audit.log.await_count == 2
    audit_calls = handler.audit.log.await_args_list
    audit_types = [c[0][1]["type"] for c in audit_calls]
    assert "tool_call" in audit_types
    assert "tool_result" in audit_types


async def test_handler_loads_history_into_messages():
    handler, mock_client = _make_handler()
    handler.conversations.get_recent.return_value = [
        {"role": "user", "content": "My name is Alice"},
        {"role": "assistant", "content": "Nice to meet you, Alice!"},
    ]
    mock_client.messages.create.return_value = _mock_text_response("Your name is Alice.")

    await handler.process(_make_message("What's my name?"))

    create_call = mock_client.messages.create.call_args
    messages_arg = create_call.kwargs.get("messages") or create_call[1].get("messages")
    assert messages_arg is not None
    assert len(messages_arg) == 3
    assert messages_arg[0]["content"] == "My name is Alice"
    assert messages_arg[2]["content"] == "What's my name?"


async def test_handler_calls_get_or_create_for_every_message():
    handler, mock_client = _make_handler()
    mock_client.messages.create.return_value = _mock_text_response("Hello!")

    await handler.process(_make_message())
    handler.tenants.get_or_create.assert_awaited_once()
