"""Tests for ReasoningService and AnthropicProvider."""
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.reasoning import (
    AnthropicProvider,
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningRequest,
    ReasoningResult,
    ReasoningService,
)
from kernos.persistence import AuditStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**kwargs) -> ReasoningRequest:
    defaults = dict(
        tenant_id="sms:+15555550100",
        conversation_id="+15555550100",
        system_prompt="You are Kernos.",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
    )
    defaults.update(kwargs)
    return ReasoningRequest(**defaults)


def _make_service(mock_provider=None, tools_for_mcp=None, read_tool_names: list[str] | None = None):
    """Return a ReasoningService with mock provider, events, mcp, audit.

    read_tool_names: MCP tool names to register as "read" in the dispatch gate registry.
    These bypass the gate automatically. Used for tests that exercise MCP tool routing.
    """
    if mock_provider is None:
        mock_provider = AsyncMock(spec=Provider)

    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None

    mcp = MagicMock()
    mcp.call_tool = AsyncMock(return_value="tool result")

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    service = ReasoningService(mock_provider, events, mcp, audit)

    # Wire a registry so the dispatch gate can classify tools
    if read_tool_names:
        cap = CapabilityInfo(
            name="test-capability",
            display_name="Test Capability",
            description="Test",
            category="test",
            status=CapabilityStatus.CONNECTED,
            tools=read_tool_names,
            server_name="test",
            tool_effects={name: "read" for name in read_tool_names},
        )
        registry = CapabilityRegistry(mcp=None)
        registry.register(cap)
        service.set_registry(registry)

    return service, mock_provider, events, mcp, audit


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _tool_response(name: str, id: str, input: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="tool_use", name=name, id=id, input=input)],
        stop_reason="tool_use",
        input_tokens=15,
        output_tokens=5,
    )


# ---------------------------------------------------------------------------
# ReasoningService — basic behavior
# ---------------------------------------------------------------------------


async def test_reason_returns_text_on_simple_response():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hello there!")

    result = await service.reason(_make_request())

    assert isinstance(result, ReasoningResult)
    assert result.text == "Hello there!"
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert result.tool_iterations == 0


async def test_reason_tool_use_loop_calls_mcp_and_continues():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["list_events"])
    mcp.call_tool.return_value = "Meeting at 10am"

    mock_provider.complete.side_effect = [
        _tool_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _text_response("You have a meeting at 10am."),
    ]

    result = await service.reason(
        _make_request(
            tools=[{"name": "list_events", "description": "List events", "input_schema": {}}]
        )
    )

    assert result.text == "You have a meeting at 10am."
    assert result.tool_iterations == 1
    mcp.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_reason_safety_valve_at_max_iterations():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["some_tool"])
    # Always return tool_use to trigger safety valve
    mock_provider.complete.return_value = _tool_response("some_tool", "tu_x", {})
    mcp.call_tool.return_value = "result"

    result = await service.reason(
        _make_request(
            tools=[{"name": "some_tool", "description": "A tool", "input_schema": {}}]
        )
    )

    assert result.tool_iterations == ReasoningService.MAX_TOOL_ITERATIONS
    assert "trouble" in result.text.lower() or "simpler" in result.text.lower()


# ---------------------------------------------------------------------------
# ReasoningService — event emission
# ---------------------------------------------------------------------------


async def test_reason_emits_reasoning_request_and_response():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hi!")

    await service.reason(_make_request())

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.REASONING_REQUEST in emitted_types
    assert EventType.REASONING_RESPONSE in emitted_types


async def test_reason_emits_tool_called_and_result():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["my_tool"])
    mcp.call_tool.return_value = "some data"

    mock_provider.complete.side_effect = [
        _tool_response("my_tool", "tu_001", {"key": "val"}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "my_tool", "description": "My tool", "input_schema": {}}]
        )
    )

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.TOOL_CALLED in emitted_types
    assert EventType.TOOL_RESULT in emitted_types


async def test_reason_reasoning_response_has_token_counts():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hi!")

    await service.reason(_make_request())

    emitted = [c.args[0] for c in events.emit.call_args_list]
    rr = next(e for e in emitted if e.type == EventType.REASONING_RESPONSE)
    assert rr.payload["input_tokens"] == 10
    assert rr.payload["output_tokens"] == 20
    assert "estimated_cost_usd" in rr.payload
    assert "duration_ms" in rr.payload


# ---------------------------------------------------------------------------
# AnthropicProvider — exception mapping
# ---------------------------------------------------------------------------


async def test_anthropic_provider_maps_timeout_to_reasoning_error():
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic.APITimeoutError(request=MagicMock())
    )

    with patch("kernos.kernel.reasoning.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningTimeoutError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_connection_error():
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )

    with patch("kernos.kernel.reasoning.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningConnectionError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_rate_limit_error():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic.RateLimitError(
            message="rate limited", response=mock_response, body=None
        )
    )

    with patch("kernos.kernel.reasoning.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningRateLimitError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_api_status_error():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic.APIStatusError(
            message="Internal Server Error", response=mock_response, body=None
        )
    )

    with patch("kernos.kernel.reasoning.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningProviderError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_returns_provider_response_on_success():
    mock_client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Hello!"
    mock_client.messages.create = AsyncMock(return_value=MagicMock(
        content=[text_block],
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=5, output_tokens=10),
    ))

    with patch("kernos.kernel.reasoning.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        result = await provider.complete("model", "system", [], [], 1024)

    assert isinstance(result, ProviderResponse)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == "Hello!"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 5
    assert result.output_tokens == 10
