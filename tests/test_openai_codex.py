"""Tests for OpenAI Codex OAuth provider and credential resolution.

Tests the chatgpt.com/backend-api/codex/responses path, NOT api.openai.com.
"""
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.credentials import (
    OpenAICodexCredential,
    _decode_jwt_account_id,
    resolve_openai_codex_credential,
)
from kernos.kernel.reasoning import (
    ContentBlock,
    OpenAICodexProvider,
    ProviderResponse,
)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_jwt(account_id: str = "acct_test123") -> str:
    """Create a minimal JWT with the expected claim structure."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        },
        "exp": int(time.time()) + 86400,
    }).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


class TestJWTAccountId:
    def test_extracts_account_id(self):
        jwt = _make_jwt("acct_abc123")
        assert _decode_jwt_account_id(jwt) == "acct_abc123"

    def test_raises_on_missing_claim(self):
        header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "user"}).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        bad_jwt = f"{header.decode()}.{payload.decode()}.{sig.decode()}"
        with pytest.raises(ValueError, match="accountId"):
            _decode_jwt_account_id(bad_jwt)

    def test_raises_on_invalid_jwt(self):
        with pytest.raises(ValueError):
            _decode_jwt_account_id("not-a-jwt")


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


class TestCodexCredentialFromEnv:
    def test_resolves_from_env(self, monkeypatch):
        jwt = _make_jwt("acct_env")
        monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", "refresh_xxx")
        monkeypatch.setenv("OPENAI_CODEX_EXPIRES", str(int(time.time() * 1000) + 86400000))
        monkeypatch.setenv("OPENAI_CODEX_ACCOUNT_ID", "acct_env")

        creds = resolve_openai_codex_credential()
        assert creds["access"] == jwt
        assert creds["refresh"] == "refresh_xxx"
        assert creds["accountId"] == "acct_env"

    def test_extracts_account_from_jwt_when_not_in_env(self, monkeypatch):
        jwt = _make_jwt("acct_from_jwt")
        monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", "refresh_xxx")
        monkeypatch.delenv("OPENAI_CODEX_ACCOUNT_ID", raising=False)

        creds = resolve_openai_codex_credential()
        assert creds["accountId"] == "acct_from_jwt"


class TestCodexCredentialFromFile:
    def test_resolves_from_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)

        jwt = _make_jwt("acct_file")
        creds_file = tmp_path / "openai-codex.json"
        creds_file.write_text(json.dumps({
            "access": jwt,
            "refresh": "refresh_file",
            "expires": int(time.time() * 1000) + 86400000,
            "accountId": "acct_file",
        }))
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))

        creds = resolve_openai_codex_credential()
        assert creds["access"] == jwt
        assert creds["accountId"] == "acct_file"

    def test_raises_when_no_source(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(tmp_path / "nonexistent.json"))

        with pytest.raises(ValueError, match="No OpenAI Codex credentials"):
            resolve_openai_codex_credential()


# ---------------------------------------------------------------------------
# Provider: input translation (Anthropic → Responses API)
# ---------------------------------------------------------------------------


class TestCodexInputTranslation:
    """_translate_input converts Anthropic messages to Responses API input items."""

    def test_plain_user_message(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": "Hello"},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "user"
        assert items[0]["content"][0]["type"] == "input_text"
        assert items[0]["content"][0]["text"] == "Hello"

    def test_plain_assistant_message(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "assistant", "content": "Hi there"},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "assistant"
        assert items[0]["content"][0]["type"] == "output_text"

    def test_tool_use_blocks(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tc_1", "name": "list-events", "input": {"date": "2026-01-01"}},
            ]},
        ])
        # Should produce: text message + function_call item
        text_items = [i for i in items if i["type"] == "message"]
        call_items = [i for i in items if i["type"] == "function_call"]
        assert len(text_items) == 1
        assert text_items[0]["content"][0]["text"] == "Let me check."
        assert len(call_items) == 1
        assert call_items[0]["call_id"] == "tc_1"
        assert call_items[0]["name"] == "list-events"
        assert json.loads(call_items[0]["arguments"]) == {"date": "2026-01-01"}

    def test_tool_result_blocks(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "Meeting at 10am"},
            ]},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "function_call_output"
        assert items[0]["call_id"] == "tc_1"
        assert items[0]["output"] == "Meeting at 10am"


# ---------------------------------------------------------------------------
# Provider: tool translation
# ---------------------------------------------------------------------------


class TestCodexToolTranslation:
    def test_translates_anthropic_format(self):
        tools = [
            {"name": "list-events", "description": "List events", "input_schema": {
                "type": "object", "properties": {"date": {"type": "string"}},
            }},
        ]
        oai = OpenAICodexProvider._translate_tools(tools)
        assert len(oai) == 1
        assert oai[0]["type"] == "function"
        assert oai[0]["name"] == "list-events"
        assert "date" in oai[0]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Provider: response parsing (Responses API format)
# ---------------------------------------------------------------------------


class TestCodexResponseParsing:
    def test_parses_text_response(self):
        data = {
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello!"}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "end_turn"
        assert len(resp.content) == 1
        assert resp.content[0].text == "Hello!"
        assert resp.input_tokens == 10

    def test_parses_tool_call_response(self):
        data = {
            "status": "completed",
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "create-event",
                "arguments": '{"title": "Meeting"}',
            }],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "tool_use"
        assert len(resp.content) == 1
        assert resp.content[0].type == "tool_use"
        assert resp.content[0].name == "create-event"
        assert resp.content[0].input == {"title": "Meeting"}
        assert resp.content[0].id == "call_1"

    def test_parses_mixed_text_and_tool(self):
        data = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Creating event."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "create-event",
                    "arguments": '{"title": "Test"}',
                },
            ],
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "tool_use"
        assert len(resp.content) == 2
        assert resp.content[0].type == "text"
        assert resp.content[1].type == "tool_use"

    def test_handles_empty_output(self):
        resp = OpenAICodexProvider._parse_response({"output": [], "status": "completed"})
        assert resp.stop_reason == "end_turn"
        assert resp.content[0].text == ""

    def test_incomplete_status_maps_to_max_tokens(self):
        data = {
            "status": "incomplete",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Partial"}]}],
            "usage": {},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# Provider: request headers
# ---------------------------------------------------------------------------


class TestCodexHeaders:
    def test_headers_include_required_fields(self):
        cred = OpenAICodexCredential(
            access="token_abc", refresh="ref", expires=0, accountId="acct_123",
        )
        provider = OpenAICodexProvider(credential=cred)
        headers = provider._headers()
        assert headers["Authorization"] == "Bearer token_abc"
        assert headers["chatgpt-account-id"] == "acct_123"
        assert headers["originator"] == "pi"
        assert headers["User-Agent"] == "pi (python)"
        assert headers["OpenAI-Beta"] == "responses=experimental"


# ---------------------------------------------------------------------------
# Provider: URL resolution
# ---------------------------------------------------------------------------


class TestCodexURL:
    def test_default_url(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert url == "https://chatgpt.com/backend-api/codex/responses"

    def test_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_CODEX_BASE_URL", "https://custom.example.com/api")
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert url == "https://custom.example.com/api/codex/responses"

    def test_url_already_has_codex_responses(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        provider._base_url = "https://chatgpt.com/backend-api/codex/responses"
        assert provider._resolve_url() == "https://chatgpt.com/backend-api/codex/responses"


# ---------------------------------------------------------------------------
# Provider: model defaults
# ---------------------------------------------------------------------------


class TestCodexModelDefaults:
    def test_default_model(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        assert provider.main_model
        assert provider.cheap_model

    def test_custom_model(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred, model="gpt-5")
        assert provider.main_model == "gpt-5"


# ---------------------------------------------------------------------------
# Provider: NOT using chat/completions
# ---------------------------------------------------------------------------


class TestCodexNotChatCompletions:
    """Verify the provider does NOT hit api.openai.com/v1/chat/completions."""

    def test_url_is_not_chat_completions(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert "chat/completions" not in url
        assert "chatgpt.com/backend-api" in url
        assert "codex/responses" in url

    def test_request_body_uses_responses_format(self):
        """The body should use 'instructions' and 'input', not 'messages'."""
        # This is verified by the _translate_input method producing Responses items
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": "Test"},
        ])
        # Responses API uses typed items, not chat messages
        assert items[0]["type"] == "message"
        assert items[0]["content"][0]["type"] == "input_text"
