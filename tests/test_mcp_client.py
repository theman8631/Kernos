from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import StdioServerParameters

from kernos.capability.client import MCPClientManager
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream


def test_register_server_stores_config():
    mcp = MCPClientManager()
    params = StdioServerParameters(command="npx", args=["test-server"], env={})
    mcp.register_server("test", params)
    assert "test" in mcp._servers
    assert mcp._servers["test"] == params


def test_get_tools_returns_empty_before_connect():
    mcp = MCPClientManager()
    assert mcp.get_tools() == []


async def test_call_tool_unknown_tool_returns_error_string():
    mcp = MCPClientManager()
    result = await mcp.call_tool("nonexistent_tool", {})
    assert isinstance(result, str)
    assert "not available" in result or "error" in result.lower()


async def test_call_tool_with_mock_session_returns_result():
    mcp = MCPClientManager()

    mock_session = AsyncMock()
    mock_content = MagicMock()
    mock_content.text = "Meeting at 10am"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result

    mcp._sessions["google-calendar"] = mock_session
    mcp._tool_to_session["list_events"] = "google-calendar"

    result = await mcp.call_tool("list_events", {"date": "2026-03-01"})
    assert result == "Meeting at 10am"
    mock_session.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_call_tool_when_mcp_errors_returns_error_string():
    mcp = MCPClientManager()

    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = RuntimeError("connection lost")

    mcp._sessions["google-calendar"] = mock_session
    mcp._tool_to_session["list_events"] = "google-calendar"

    result = await mcp.call_tool("list_events", {})
    assert isinstance(result, str)
    assert "error" in result.lower()
    # Must not raise — graceful string return is the contract


# ---------------------------------------------------------------------------
# connect_all() — capability event emission (AC6, AC15)
# ---------------------------------------------------------------------------


def _make_mock_tool(name: str) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.description = f"Tool {name}"
    t.inputSchema = {}
    return t


async def test_connect_all_emits_capability_connected(tmp_path):
    """AC6: successful connect emits capability.connected with tool_count and tool_names."""
    events = JsonEventStream(tmp_path)
    mcp = MCPClientManager(events=events)
    mcp.register_server(
        "test-server", StdioServerParameters(command="npx", args=["srv"], env={})
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.tools = [_make_mock_tool("tool_a"), _make_mock_tool("tool_b")]
    mock_session.list_tools.return_value = mock_result

    @asynccontextmanager
    async def _mock_stdio(params):
        yield AsyncMock(), AsyncMock()

    @asynccontextmanager
    async def _mock_client_session(read, write):
        yield mock_session

    with patch("kernos.capability.client.stdio_client", side_effect=_mock_stdio), patch(
        "kernos.capability.client.ClientSession", side_effect=_mock_client_session
    ):
        await mcp.connect_all()

    emitted = await events.query("system")
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev.type == EventType.CAPABILITY_CONNECTED
    assert ev.payload["server_name"] == "test-server"
    assert ev.payload["tool_count"] == 2
    assert "tool_a" in ev.payload["tool_names"]
    assert "tool_b" in ev.payload["tool_names"]
    assert ev.metadata == {}


async def test_connect_all_emits_capability_error_on_failure(tmp_path):
    """AC6: failed connect emits capability.error with the error message."""
    events = JsonEventStream(tmp_path)
    mcp = MCPClientManager(events=events)
    mcp.register_server(
        "bad-server", StdioServerParameters(command="npx", args=["srv"], env={})
    )

    with patch(
        "kernos.capability.client.stdio_client",
        side_effect=RuntimeError("connection refused"),
    ):
        await mcp.connect_all()

    emitted = await events.query("system")
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev.type == EventType.CAPABILITY_ERROR
    assert ev.payload["server_name"] == "bad-server"
    assert "connection refused" in ev.payload["error"]
    assert ev.metadata == {}


# ---------------------------------------------------------------------------
# Per-tool timeout (Fix 2)
# ---------------------------------------------------------------------------


async def test_call_tool_timeout_returns_clean_error():
    """AC5: Timeout wraps MCP call and returns clean error string."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    async def _slow_tool(name, args):
        import asyncio
        await asyncio.sleep(10)

    mock_session.call_tool = _slow_tool
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["slow_tool"] = "srv"

    result = await mcp.call_tool("slow_tool", {}, timeout=0.05)
    assert "timed out" in result
    assert "slow_tool" in result


async def test_call_tool_cancelled_error_propagates():
    """AC7: CancelledError is never swallowed — always re-raised."""
    import asyncio as _asyncio
    mcp = MCPClientManager()
    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = _asyncio.CancelledError()
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["t1"] = "srv"

    with pytest.raises(_asyncio.CancelledError):
        await mcp.call_tool("t1", {})


# ---------------------------------------------------------------------------
# Automatic retry with backoff (Fix 3)
# ---------------------------------------------------------------------------


async def test_transient_failure_retries_then_succeeds():
    """AC8: Transient failure (timeout) retries once and succeeds."""
    import asyncio as _asyncio
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    call_count = 0
    mock_content = MagicMock()
    mock_content.text = "success"
    mock_result = MagicMock()
    mock_result.content = [mock_content]

    async def _flaky_tool(name, args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _asyncio.TimeoutError()
        return mock_result

    mock_session.call_tool = _flaky_tool
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["flaky"] = "srv"

    # Use short backoff for test speed — patch at module level
    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01
    try:
        result = await mcp.call_tool("flaky", {}, timeout=0.05)
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    assert result == "success"
    assert call_count == 2


async def test_non_transient_failure_does_not_retry():
    """AC9: Non-transient failure (validation error) does not retry."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    call_count = 0
    async def _bad_tool(name, args):
        nonlocal call_count
        call_count += 1
        raise ValueError("Invalid parameter 'date': validation failed")

    mock_session.call_tool = _bad_tool
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["bad"] = "srv"

    result = await mcp.call_tool("bad", {})
    assert call_count == 1  # No retry
    assert "error" in result.lower()


def test_is_transient_classification():
    """Test _is_transient classifies correctly."""
    import asyncio as _asyncio
    assert MCPClientManager._is_transient("503 service unavailable") is True
    assert MCPClientManager._is_transient("429 rate limit exceeded") is True
    assert MCPClientManager._is_transient("connection refused") is True
    assert MCPClientManager._is_transient("timeout") is True
    assert MCPClientManager._is_transient("", _asyncio.TimeoutError()) is True
    assert MCPClientManager._is_transient("", ConnectionResetError()) is True

    # Not transient
    assert MCPClientManager._is_transient("tool not found") is False
    assert MCPClientManager._is_transient("permission denied") is False
    assert MCPClientManager._is_transient("invalid parameter") is False
    assert MCPClientManager._is_transient("unauthorized") is False


async def test_timeout_override_per_tool():
    """Timeout overrides apply per-tool."""
    mcp = MCPClientManager()
    assert mcp._get_timeout("goto") == 45
    assert mcp._get_timeout("brave_web_search") == 15
    assert mcp._get_timeout("get-current-time") == 5
    assert mcp._get_timeout("some-random-tool") == 30
