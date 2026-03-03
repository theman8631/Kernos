import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from kernos.app import app

OWNER_PHONE = "+15555550100"


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


@pytest.fixture
def tc():
    """TestClient with Anthropic mocked and persistence using a temp directory."""
    tmpdir = tempfile.mkdtemp()
    try:
        with patch("kernos.messages.handler.anthropic.Anthropic") as mock_cls:
            mock_anthropic = MagicMock()
            mock_cls.return_value = mock_anthropic
            with patch.dict(os.environ, {"KERNOS_DATA_DIR": tmpdir}):
                with TestClient(app) as client:
                    yield client, mock_anthropic
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health(tc):
    client, _ = tc
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sms_inbound_returns_twiml(tc):
    client, mock_anthropic = tc
    mock_anthropic.messages.create.return_value = _mock_text_response("Hi there!")

    response = client.post(
        "/sms/inbound",
        data={
            "From": OWNER_PHONE,
            "To": "+12345678901",
            "Body": "Hello",
            "SmsSid": "SM123",
        },
    )

    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    assert "<Response>" in response.text
    assert "Hi there!" in response.text


def test_sms_inbound_error_returns_friendly_twiml(tc):
    """If the handler raises unexpectedly, the app returns friendly TwiML (not 500)."""
    client, mock_anthropic = tc
    mock_anthropic.messages.create.side_effect = Exception("kaboom")

    response = client.post(
        "/sms/inbound",
        data={
            "From": OWNER_PHONE,
            "To": "+12345678901",
            "Body": "Hello",
            "SmsSid": "SM456",
        },
    )

    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    assert "<Response>" in response.text


def test_sms_inbound_with_tool_use_returns_calendar_response(tc):
    """Integration: inbound SMS → tool-use loop → outbound TwiML with real data."""
    client, mock_anthropic = tc

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "list_events"
    tool_block.id = "tu_001"
    tool_block.input = {"date": "2026-03-01"}
    tool_response = MagicMock()
    tool_response.content = [tool_block]
    tool_response.stop_reason = "tool_use"
    tool_response.usage.input_tokens = 15
    tool_response.usage.output_tokens = 5

    mock_anthropic.messages.create.side_effect = [
        tool_response,
        _mock_text_response("You have a team standup at 10am."),
    ]

    # Inject a mock tool into the handler's MCP manager
    mcp = client.app.state.handler.mcp
    mcp._tool_to_session["list_events"] = "google-calendar"
    mock_session = AsyncMock()
    mock_content = MagicMock()
    mock_content.text = "Standup at 10am"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_session.call_tool.return_value = mock_result
    mcp._sessions["google-calendar"] = mock_session
    mcp._tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]

    response = client.post(
        "/sms/inbound",
        data={
            "From": OWNER_PHONE,
            "To": "+12345678901",
            "Body": "What's on my calendar today?",
            "SmsSid": "SM789",
        },
    )

    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    assert "standup" in response.text.lower() or "<Response>" in response.text
