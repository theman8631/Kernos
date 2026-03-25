"""Tests for kernos.kernel.credentials — Anthropic credential resolution."""

import json
from unittest.mock import MagicMock, patch

from kernos.kernel.credentials import resolve_anthropic_credential


def _no_cli(func):
    """Patch Claude CLI credential to return None for tests of lower-priority sources.

    Without this, the real ~/.claude/.credentials.json on the dev machine would
    resolve before OpenClaw or empty-fallback tests can exercise their paths.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with patch("kernos.kernel.credentials._read_claude_cli_credential", return_value=None):
            return func(*args, **kwargs)

    return wrapper


def _make_openclaw_file(tmp_path, profiles_data):
    """Write an auth-profiles.json and return its path."""
    path = tmp_path / "auth-profiles.json"
    path.write_text(json.dumps(profiles_data))
    return str(path)


def test_api_key_returns_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key123")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)
    assert resolve_anthropic_credential() == "sk-ant-key123"


def test_oauth_token_when_api_key_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "sk-ant-oat01-abc")
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)
    assert resolve_anthropic_credential() == "sk-ant-oat01-abc"


def test_api_key_takes_priority_over_oauth(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key123")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "sk-ant-oat01-abc")
    assert resolve_anthropic_credential() == "sk-ant-key123"


@_no_cli
def test_openclaw_token_type(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = _make_openclaw_file(tmp_path, {
        "lastGood": {"anthropic": "anthropic:default"},
        "profiles": {
            "anthropic:default": {"type": "token", "token": "sk-ant-oat01-fromclaw"},
        },
    })
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", path)
    assert resolve_anthropic_credential() == "sk-ant-oat01-fromclaw"


@_no_cli
def test_openclaw_api_key_type(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = _make_openclaw_file(tmp_path, {
        "lastGood": {"anthropic": "anthropic:work"},
        "profiles": {
            "anthropic:work": {"type": "api_key", "key": "sk-ant-workkey"},
        },
    })
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", path)
    assert resolve_anthropic_credential() == "sk-ant-workkey"


@_no_cli
def test_openclaw_file_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", "/nonexistent/auth-profiles.json")
    assert resolve_anthropic_credential() == ""


@_no_cli
def test_openclaw_file_malformed(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = tmp_path / "auth-profiles.json"
    path.write_text("not valid json{{{")
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", str(path))
    assert resolve_anthropic_credential() == ""


@_no_cli
def test_openclaw_no_anthropic_in_lastgood(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = _make_openclaw_file(tmp_path, {
        "lastGood": {"openai": "openai:default"},
        "profiles": {},
    })
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", path)
    assert resolve_anthropic_credential() == ""


@_no_cli
def test_nothing_set_returns_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)
    assert resolve_anthropic_credential() == ""


def test_claude_cli_credential_resolves(monkeypatch, tmp_path):
    """Claude CLI credentials file is read when env vars are empty."""
    import time
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)

    creds = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-cli-token",
            "refreshToken": "refresh-xxx",
            "expiresAt": int((time.time() + 86400) * 1000),  # 24h from now
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        }
    }
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text(json.dumps(creds))

    # Mock the HTTP key exchange to return a fake API key
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"api_key": "sk-ant-exchanged-key"}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("kernos.kernel.credentials.os.path.expanduser", return_value=str(creds_path)), \
         patch("urllib.request.urlopen", return_value=mock_response):
        assert resolve_anthropic_credential() == "sk-ant-exchanged-key"


@_no_cli
def test_claude_cli_expired_falls_through(monkeypatch, tmp_path):
    """Expired Claude CLI token falls through to next priority."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)
    assert resolve_anthropic_credential() == ""
