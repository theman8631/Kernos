from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningService,
)
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


def _mock_provider_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _mock_provider_tool_response(name: str, id: str, input: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="tool_use", name=name, id=id, input=input)],
        stop_reason="tool_use",
        input_tokens=15,
        output_tokens=5,
    )


def _make_mock_registry(tools: list[dict] | None = None) -> MagicMock:
    """Return a mock CapabilityRegistry."""
    registry = MagicMock(spec=CapabilityRegistry)
    registry.get_connected_tools.return_value = tools or []
    registry.build_capability_prompt.return_value = (
        "CURRENT CAPABILITIES — conversation only." if not tools
        else "CONNECTED CAPABILITIES — you can use these:\n- Calendar tools available."
    )
    registry.get_all.return_value = []
    return registry


def _make_handler(tools: list[dict] | None = None) -> tuple[MessageHandler, AsyncMock]:
    """Return a (handler, mock_provider) with mock MCP, stores, events, state, and registry."""
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
    state.save_tenant_profile.return_value = None

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry(tools)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry)
    return handler, mock_provider


# --- Happy path ---


async def test_process_returns_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert result == "Hi!"


# --- Error paths: each must return a friendly string, never raise ---


async def test_timeout_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_connection_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningConnectionError("connection failed")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_rate_limit_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningRateLimitError("rate limited")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "overloaded" in result.lower() or "minute" in result.lower()


async def test_api_status_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningProviderError("API status 500: Internal Server Error")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_unexpected_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = RuntimeError("something broke")

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

    cap_prompt = "CURRENT CAPABILITIES — conversation only."

    discord_msg = NormalizedMessage(**base, platform="discord")
    assert "Discord" in _build_system_prompt(discord_msg, cap_prompt)
    assert "SMS" not in _build_system_prompt(discord_msg, cap_prompt)

    sms_msg = NormalizedMessage(**base, platform="sms")
    assert "SMS" in _build_system_prompt(sms_msg, cap_prompt)
    assert "Discord" not in _build_system_prompt(sms_msg, cap_prompt)


def test_system_prompt_includes_capability_prompt():
    msg = _make_message()
    cap_prompt = "CONNECTED CAPABILITIES — you can use these:\n- Google Calendar."
    prompt = _build_system_prompt(msg, cap_prompt)
    assert "CONNECTED CAPABILITIES" in prompt
    assert "Google Calendar" in prompt


def test_system_prompt_includes_conversation_only_when_no_caps():
    msg = _make_message()
    cap_prompt = "CURRENT CAPABILITIES — conversation only."
    prompt = _build_system_prompt(msg, cap_prompt)
    assert "conversation only" in prompt.lower()


def test_system_prompt_does_not_claim_cannot_remember():
    msg = _make_message()
    prompt = _build_system_prompt(msg, "CURRENT CAPABILITIES — conversation only.")
    assert "cannot remember previous conversations" not in prompt


def test_handler_uses_registry_for_capability_prompt():
    """Verify handler calls registry.build_capability_prompt() not hardcoded logic."""
    handler, mock_provider = _make_handler()
    handler.registry.build_capability_prompt.return_value = "CUSTOM PROMPT FROM REGISTRY"
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    import asyncio
    asyncio.get_event_loop().run_until_complete(handler.process(_make_message()))

    handler.registry.build_capability_prompt.assert_called()


# --- MCP tool-use loop ---


async def test_handler_with_tool_use_brokers_call_and_returns_text():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_provider.complete.side_effect = [
        _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_provider_response("You have a meeting at 10am."),
    ]

    result = await handler.process(_make_message("What's on my schedule?"))
    assert "10am" in result
    handler.mcp.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_handler_safety_valve_returns_graceful_message():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="some result")

    mock_provider.complete.return_value = _mock_provider_tool_response(
        "list_events", "tu_001", {}
    )

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert "trouble" in result.lower() or "simpler" in result.lower()


async def test_handler_tool_failure_results_in_graceful_response():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Calendar tool error: connection failed")

    mock_provider.complete.side_effect = [
        _mock_provider_tool_response("list_events", "tu_001", {}),
        _mock_provider_response("I had trouble checking your calendar."),
    ]

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert len(result) > 0


async def test_handler_no_tools_works_identically_to_1a2():
    handler, mock_provider = _make_handler(tools=[])
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    result = await handler.process(_make_message())
    assert result == "Hello!"
    assert mock_provider.complete.call_count == 1


# --- Persistence (AC19) ---


async def test_handler_stores_user_and_assistant_messages():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("I'm good, thanks!")

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
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    mock_provider.complete.side_effect = [
        _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _mock_provider_response("You have a meeting at 10am."),
    ]

    await handler.process(_make_message("What's on my schedule?"))

    assert handler.conversations.append.await_count == 2
    assert handler.audit.log.await_count == 2
    audit_calls = handler.audit.log.await_args_list
    audit_types = [c[0][1]["type"] for c in audit_calls]
    assert "tool_call" in audit_types
    assert "tool_result" in audit_types


async def test_handler_loads_history_into_messages():
    handler, mock_provider = _make_handler()
    handler.conversations.get_recent.return_value = [
        {"role": "user", "content": "My name is Alice"},
        {"role": "assistant", "content": "Nice to meet you, Alice!"},
    ]
    mock_provider.complete.return_value = _mock_provider_response("Your name is Alice.")

    await handler.process(_make_message("What's my name?"))

    complete_call = mock_provider.complete.call_args
    messages_arg = complete_call.kwargs.get("messages") or complete_call[1].get("messages")
    assert messages_arg is not None
    assert len(messages_arg) == 3
    assert messages_arg[0]["content"] == "My name is Alice"
    assert messages_arg[2]["content"] == "What's my name?"


async def test_handler_calls_get_or_create_for_every_message():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    await handler.process(_make_message())
    handler.tenants.get_or_create.assert_awaited_once()
