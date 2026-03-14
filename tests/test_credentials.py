"""Tests for kernos.kernel.credentials — Anthropic credential resolution."""

import json

from kernos.kernel.credentials import resolve_anthropic_credential


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


def test_openclaw_file_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", "/nonexistent/auth-profiles.json")
    assert resolve_anthropic_credential() == ""


def test_openclaw_file_malformed(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = tmp_path / "auth-profiles.json"
    path.write_text("not valid json{{{")
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", str(path))
    assert resolve_anthropic_credential() == ""


def test_openclaw_no_anthropic_in_lastgood(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    path = _make_openclaw_file(tmp_path, {
        "lastGood": {"openai": "openai:default"},
        "profiles": {},
    })
    monkeypatch.setenv("OPENCLAW_AUTH_PROFILES_PATH", path)
    assert resolve_anthropic_credential() == ""


def test_nothing_set_returns_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_PROFILES_PATH", raising=False)
    assert resolve_anthropic_credential() == ""
