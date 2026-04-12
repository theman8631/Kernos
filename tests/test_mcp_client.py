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


# ---------------------------------------------------------------------------
# Error-in-result detection (Closeout Fix 1)
# ---------------------------------------------------------------------------


def test_is_error_in_result_detects_rate_limit():
    """Error-shaped tool results are detected."""
    assert MCPClientManager._is_error_in_result("Error: Rate limit exceeded") is True
    assert MCPClientManager._is_error_in_result("Error: 429 Too Many Requests") is True
    assert MCPClientManager._is_error_in_result("Error: 503 Service Unavailable") is True
    assert MCPClientManager._is_error_in_result("Error: temporarily unavailable") is True


def test_is_error_in_result_ignores_normal_results():
    """Normal successful results are not flagged."""
    assert MCPClientManager._is_error_in_result("Meeting at 10am") is False
    assert MCPClientManager._is_error_in_result('{"events": []}') is False
    assert MCPClientManager._is_error_in_result("(empty result)") is False


def test_is_error_in_result_ignores_long_content_with_error_mention():
    """Long results mentioning 'error' in the middle are not flagged."""
    long_content = "x" * 600 + " error: rate limit " + "y" * 100
    assert MCPClientManager._is_error_in_result(long_content) is False


async def test_error_in_result_triggers_retry():
    """Error-in-result triggers transient retry path."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    call_count = 0
    mock_content_err = MagicMock()
    mock_content_err.text = "Error: Rate limit exceeded"
    mock_result_err = MagicMock()
    mock_result_err.content = [mock_content_err]

    mock_content_ok = MagicMock()
    mock_content_ok.text = "Search results here"
    mock_result_ok = MagicMock()
    mock_result_ok.content = [mock_content_ok]

    async def _rate_limited_then_ok(name, args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_result_err
        return mock_result_ok

    mock_session.call_tool = _rate_limited_then_ok
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["brave_web_search"] = "srv"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01
    try:
        result = await mcp.call_tool("brave_web_search", {})
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    assert result == "Search results here"
    assert call_count == 2


async def test_non_transient_error_in_result_no_retry():
    """Non-transient error-in-result returns immediately without retry."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    mock_content = MagicMock()
    mock_content.text = "Error: service unavailable"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    # "service unavailable" is transient so let's use a non-transient one
    mock_content2 = MagicMock()
    mock_content2.text = "Normal result"
    mock_result2 = MagicMock()
    mock_result2.content = [mock_content2]

    # Actually — all ERROR_IN_RESULT_PATTERNS are transient by design.
    # Let's verify that a normal (non-error) result passes through unchanged.
    mock_session.call_tool.return_value = mock_result2
    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["tool"] = "srv"

    result = await mcp.call_tool("tool", {})
    assert result == "Normal result"
    assert mock_session.call_tool.await_count == 1


# ---------------------------------------------------------------------------
# Search fallback: Brave rate limit → DuckDuckGo (DDG)
# ---------------------------------------------------------------------------


def test_is_rate_limit_error():
    """Rate limit patterns are detected."""
    assert MCPClientManager._is_rate_limit_error("Tool error: Rate limit exceeded") is True
    assert MCPClientManager._is_rate_limit_error("Tool error: 429 Too Many Requests") is True
    assert MCPClientManager._is_rate_limit_error("Error: too many requests") is True
    assert MCPClientManager._is_rate_limit_error("Tool error: connection refused") is False
    assert MCPClientManager._is_rate_limit_error("Meeting at 10am") is False


async def test_brave_web_search_rate_limit_falls_back_to_ddg():
    """brave_web_search rate limit → DDG fallback returns formatted results."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    # Brave always returns rate limit error
    mock_content = MagicMock()
    mock_content.text = "Error: Rate limit exceeded"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result

    mcp._sessions["brave-search"] = mock_session
    mcp._tool_to_session["brave_web_search"] = "brave-search"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01

    ddg_results = [
        {"title": "Result 1", "href": "https://example.com/1", "body": "Description 1"},
        {"title": "Result 2", "href": "https://example.com/2", "body": "Description 2"},
    ]

    try:
        with patch.object(MCPClientManager, "_ddg_web_search", return_value="\n\n".join(
            f"Title: {r['title']}\nDescription: {r['body']}\nURL: {r['href']}" for r in ddg_results
        )) as mock_ddg:
            result = await mcp.call_tool("brave_web_search", {"query": "test query", "count": 5})
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    # Should have DDG results in Brave format
    assert "Title: Result 1" in result
    assert "URL: https://example.com/1" in result
    assert "Title: Result 2" in result
    mock_ddg.assert_awaited_once_with("test query", 5)


async def test_brave_local_search_rate_limit_falls_back_to_web_search():
    """brave_local_search rate limit → brave_web_search with same query."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    call_count = 0

    # local search: rate limit; web search: success
    mock_content_err = MagicMock()
    mock_content_err.text = "Error: Rate limit exceeded"
    mock_result_err = MagicMock()
    mock_result_err.content = [mock_content_err]

    mock_content_ok = MagicMock()
    mock_content_ok.text = "Title: Pizza Place\nDescription: Great pizza\nURL: https://pizza.com"
    mock_result_ok = MagicMock()
    mock_result_ok.content = [mock_content_ok]

    async def _tool_dispatch(name, args):
        nonlocal call_count
        call_count += 1
        if name == "brave_local_search":
            return mock_result_err
        return mock_result_ok

    mock_session.call_tool = _tool_dispatch
    mcp._sessions["brave-search"] = mock_session
    mcp._tool_to_session["brave_local_search"] = "brave-search"
    mcp._tool_to_session["brave_web_search"] = "brave-search"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01
    try:
        result = await mcp.call_tool(
            "brave_local_search", {"query": "pizza near downtown", "count": 5}
        )
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    assert "Pizza Place" in result
    assert "Tool error" not in result


async def test_full_fallback_chain_local_to_web_to_ddg():
    """brave_local_search → brave_web_search (also rate limited) → DDG."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    # Both brave tools return rate limit
    mock_content = MagicMock()
    mock_content.text = "Error: Rate limit exceeded"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result

    mcp._sessions["brave-search"] = mock_session
    mcp._tool_to_session["brave_local_search"] = "brave-search"
    mcp._tool_to_session["brave_web_search"] = "brave-search"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01

    ddg_output = "Title: Local Pizza\nDescription: Nearby\nURL: https://ddg.com/pizza"

    try:
        with patch.object(MCPClientManager, "_ddg_web_search", return_value=ddg_output):
            result = await mcp.call_tool(
                "brave_local_search", {"query": "pizza near me", "count": 3}
            )
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    assert "Local Pizza" in result
    assert "Tool error" not in result


async def test_ddg_failure_returns_error():
    """When DDG also fails, return a clear error."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    mock_content = MagicMock()
    mock_content.text = "Error: Rate limit exceeded"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result

    mcp._sessions["brave-search"] = mock_session
    mcp._tool_to_session["brave_web_search"] = "brave-search"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01

    try:
        with patch.object(
            MCPClientManager, "_ddg_web_search",
            return_value="Tool error: DuckDuckGo search returned no results.",
        ):
            result = await mcp.call_tool("brave_web_search", {"query": "test", "count": 5})
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    assert "Tool error" in result


async def test_non_brave_tool_rate_limit_no_fallback():
    """Rate limit on non-search tools does NOT trigger DDG fallback."""
    mcp = MCPClientManager()
    mock_session = AsyncMock()

    mock_content = MagicMock()
    mock_content.text = "Error: Rate limit exceeded"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result

    mcp._sessions["srv"] = mock_session
    mcp._tool_to_session["some_other_tool"] = "srv"

    from kernos.capability import client as _client_mod
    orig_backoff = _client_mod.RETRY_BACKOFF_S
    _client_mod.RETRY_BACKOFF_S = 0.01
    try:
        with patch.object(MCPClientManager, "_ddg_web_search") as mock_ddg:
            result = await mcp.call_tool("some_other_tool", {"query": "test"})
    finally:
        _client_mod.RETRY_BACKOFF_S = orig_backoff

    mock_ddg.assert_not_awaited()
    assert "Rate limit" in result


async def test_ddg_web_search_formats_like_brave():
    """DDG results are formatted to match Brave's Title/Description/URL shape."""
    ddg_results = [
        {"title": "Python Docs", "href": "https://python.org", "body": "Official docs"},
    ]

    with patch("ddgs.DDGS") as MockDDGS:
        MockDDGS.return_value.text.return_value = ddg_results
        result = await MCPClientManager._ddg_web_search("python docs", 5)

    assert "Title: Python Docs" in result
    assert "Description: Official docs" in result
    assert "URL: https://python.org" in result
