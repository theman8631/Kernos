from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters

from kernos.capability.client import MCPClientManager


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
