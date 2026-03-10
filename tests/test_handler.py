from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
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
from kernos.kernel.task import TaskStatus, TaskType
from kernos.kernel.soul import Soul
from kernos.kernel.template import PRIMARY_TEMPLATE
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
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
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
    # Return a hatched soul with user_name set so name-ask doesn't fire.
    # Tests that specifically test first-interaction behavior use their own setup.
    state.get_soul.return_value = Soul(
        tenant_id="sms:+15555550100",
        user_name="TestUser",
        hatched=True,
        interaction_count=5,
    )
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.increment_topic_hint.return_value = None
    state.get_topic_hint_count.return_value = 0
    state.clear_topic_hint.return_value = None
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry(tools)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
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
    soul = Soul(tenant_id="t1")

    discord_msg = NormalizedMessage(**base, platform="discord")
    assert "Discord" in _build_system_prompt(discord_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "SMS" not in _build_system_prompt(discord_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])

    sms_msg = NormalizedMessage(**base, platform="sms")
    assert "SMS" in _build_system_prompt(sms_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "Discord" not in _build_system_prompt(sms_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])


def test_system_prompt_includes_capability_prompt():
    msg = _make_message()
    soul = Soul(tenant_id="t1")
    cap_prompt = "CONNECTED CAPABILITIES — you can use these:\n- Google Calendar."
    prompt = _build_system_prompt(msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "CONNECTED CAPABILITIES" in prompt
    assert "Google Calendar" in prompt


def test_system_prompt_includes_conversation_only_when_no_caps():
    msg = _make_message()
    soul = Soul(tenant_id="t1")
    cap_prompt = "CURRENT CAPABILITIES — conversation only."
    prompt = _build_system_prompt(msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "conversation only" in prompt.lower()


def test_system_prompt_does_not_claim_cannot_remember():
    msg = _make_message()
    soul = Soul(tenant_id="t1")
    prompt = _build_system_prompt(msg, "CURRENT CAPABILITIES — conversation only.", soul, PRIMARY_TEMPLATE, [])
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
    # In v2, message history comes from get_space_thread (via _assemble_space_context)
    handler.conversations.get_space_thread.return_value = [
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


# --- Task creation ---


async def test_handler_creates_task_via_engine():
    """Handler delegates to TaskEngine — mock engine to verify task fields."""
    mcp = MagicMock(spec=MCPClientManager)
    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None
    tenants = AsyncMock(spec=TenantStore)
    tenants.get_or_create.return_value = {"tenant_id": "sms:+15555550100", "status": "active", "created_at": "2026-03-01T00:00:00Z", "capabilities": {}}
    audit = AsyncMock(spec=AuditStore)
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    state = AsyncMock(spec=StateStore)
    from kernos.kernel.state import TenantProfile
    state.get_tenant_profile.return_value = TenantProfile(tenant_id="sms:+15555550100", status="active", created_at="2026-03-01T00:00:00Z")
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_tenant_profile.return_value = None
    state.get_soul.return_value = None
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.increment_topic_hint.return_value = None
    state.get_topic_hint_count.return_value = 0
    state.clear_topic_hint.return_value = None

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry()
    reasoning = ReasoningService(mock_provider, events, mcp, audit)

    # Use a mock engine to capture what task was passed
    mock_engine = AsyncMock(spec=TaskEngine)
    from kernos.kernel.task import Task, TaskStatus
    captured_task = Task(
        id="task_captured",
        type=TaskType.REACTIVE_SIMPLE,
        tenant_id="sms:+15555550100",
        conversation_id="+15555550100",
        status=TaskStatus.COMPLETED,
        result_text="Mock response",
    )
    mock_engine.execute.return_value = captured_task

    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, mock_engine)

    await handler.process(_make_message("Hello from handler"))

    mock_engine.execute.assert_awaited_once()
    call_args = mock_engine.execute.call_args
    task_arg = call_args[0][0]  # first positional arg
    assert task_arg.type == TaskType.REACTIVE_SIMPLE
    assert task_arg.tenant_id == "sms:+15555550100"
    assert task_arg.source == "user_message"
    assert task_arg.input_text == "Hello from handler"


async def test_handler_uses_task_result_text_as_response():
    """Handler reads task.result_text, not result.text."""
    mcp = MagicMock(spec=MCPClientManager)
    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None
    tenants = AsyncMock(spec=TenantStore)
    tenants.get_or_create.return_value = {"tenant_id": "sms:+15555550100", "status": "active", "created_at": "2026-03-01T00:00:00Z", "capabilities": {}}
    audit = AsyncMock(spec=AuditStore)
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    state = AsyncMock(spec=StateStore)
    from kernos.kernel.state import TenantProfile
    state.get_tenant_profile.return_value = TenantProfile(tenant_id="sms:+15555550100", status="active", created_at="2026-03-01T00:00:00Z")
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_tenant_profile.return_value = None
    state.get_soul.return_value = Soul(
        tenant_id="sms:+15555550100", user_name="TestUser", hatched=True, interaction_count=5
    )
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.increment_topic_hint.return_value = None
    state.get_topic_hint_count.return_value = 0
    state.clear_topic_hint.return_value = None
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry()
    reasoning = ReasoningService(mock_provider, events, mcp, audit)

    mock_engine = AsyncMock(spec=TaskEngine)
    from kernos.kernel.task import Task, TaskStatus
    mock_engine.execute.return_value = Task(
        id="task_x",
        type=TaskType.REACTIVE_SIMPLE,
        tenant_id="sms:+15555550100",
        conversation_id="+15555550100",
        status=TaskStatus.COMPLETED,
        result_text="The answer from the task",
    )

    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, mock_engine)

    response = await handler.process(_make_message())
    assert response == "The answer from the task"
