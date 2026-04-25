"""Tests for the oauth section in service descriptors (OAuth C1)."""

import pytest

from kernos.kernel.services import (
    AuthType,
    OAuthDeviceCodeConfig,
    PkceMode,
    ServiceDescriptorError,
    parse_service_descriptor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _oauth_descriptor(**overrides):
    raw = {
        "service_id": "slack",
        "display_name": "Slack",
        "auth_type": "oauth_device_code",
        "operations": ["read_messages", "post_message"],
        "oauth": {
            "device_authorization_uri": "https://slack.com/oauth/device",
            "token_uri": "https://slack.com/oauth/token",
            "client_id": "123.456",
        },
    }
    raw.update(overrides)
    return raw


def _api_token_descriptor(**overrides):
    raw = {
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages"],
    }
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_oauth_section_parses_with_literal_client_id():
    desc = parse_service_descriptor(_oauth_descriptor())
    assert desc.oauth is not None
    assert desc.oauth.device_authorization_uri == "https://slack.com/oauth/device"
    assert desc.oauth.token_uri == "https://slack.com/oauth/token"
    assert desc.oauth.client_id == "123.456"
    assert desc.oauth.client_id_env == ""
    assert desc.oauth.pkce == PkceMode.OPTIONAL


def test_oauth_section_parses_with_env_var_client_id():
    raw = _oauth_descriptor(oauth={
        "device_authorization_uri": "https://example.com/device",
        "token_uri": "https://example.com/token",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "pkce": "required",
    })
    desc = parse_service_descriptor(raw)
    assert desc.oauth.client_id == ""
    assert desc.oauth.client_id_env == "GOOGLE_OAUTH_CLIENT_ID"
    assert desc.oauth.pkce == PkceMode.REQUIRED


def test_oauth_section_pkce_modes():
    for mode in ("required", "optional", "omit"):
        raw = _oauth_descriptor()
        raw["oauth"]["pkce"] = mode
        desc = parse_service_descriptor(raw)
        assert desc.oauth.pkce.value == mode


# ---------------------------------------------------------------------------
# Validation rejects
# ---------------------------------------------------------------------------


def test_oauth_section_rejected_for_api_token_service():
    raw = _api_token_descriptor()
    raw["oauth"] = {
        "device_authorization_uri": "https://x",
        "token_uri": "https://y",
        "client_id": "abc",
    }
    with pytest.raises(ServiceDescriptorError, match="only valid for"):
        parse_service_descriptor(raw)


def test_oauth_section_required_for_device_code_service():
    raw = _oauth_descriptor()
    raw.pop("oauth")
    with pytest.raises(ServiceDescriptorError, match="no oauth section"):
        parse_service_descriptor(raw)


def test_oauth_section_must_be_dict():
    raw = _oauth_descriptor(oauth="not a dict")
    with pytest.raises(ServiceDescriptorError, match="must be a dict"):
        parse_service_descriptor(raw)


def test_oauth_section_missing_device_authorization_uri():
    raw = _oauth_descriptor()
    raw["oauth"].pop("device_authorization_uri")
    with pytest.raises(
        ServiceDescriptorError, match="device_authorization_uri",
    ):
        parse_service_descriptor(raw)


def test_oauth_section_missing_token_uri():
    raw = _oauth_descriptor()
    raw["oauth"].pop("token_uri")
    with pytest.raises(ServiceDescriptorError, match="token_uri"):
        parse_service_descriptor(raw)


def test_oauth_section_must_have_exactly_one_client_id_source():
    raw = _oauth_descriptor()
    raw["oauth"]["client_id_env"] = "BOTH_SET"
    with pytest.raises(ServiceDescriptorError, match="both client_id and client_id_env"):
        parse_service_descriptor(raw)


def test_oauth_section_must_have_some_client_id_source():
    raw = _oauth_descriptor()
    raw["oauth"].pop("client_id")
    with pytest.raises(ServiceDescriptorError, match="exactly one of"):
        parse_service_descriptor(raw)


def test_oauth_section_unknown_pkce_value_rejected():
    raw = _oauth_descriptor()
    raw["oauth"]["pkce"] = "neighborly"
    with pytest.raises(ServiceDescriptorError, match="pkce"):
        parse_service_descriptor(raw)


# ---------------------------------------------------------------------------
# resolve_client_id
# ---------------------------------------------------------------------------


def test_resolve_client_id_returns_literal():
    desc = parse_service_descriptor(_oauth_descriptor())
    assert desc.resolve_client_id() == "123.456"


def test_resolve_client_id_reads_env_var(monkeypatch):
    monkeypatch.setenv("EXAMPLE_OAUTH_CLIENT", "from-env-12345")
    raw = _oauth_descriptor(oauth={
        "device_authorization_uri": "https://example.com/device",
        "token_uri": "https://example.com/token",
        "client_id_env": "EXAMPLE_OAUTH_CLIENT",
    })
    desc = parse_service_descriptor(raw)
    assert desc.resolve_client_id() == "from-env-12345"


def test_resolve_client_id_raises_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("EXAMPLE_OAUTH_CLIENT", raising=False)
    raw = _oauth_descriptor(oauth={
        "device_authorization_uri": "https://example.com/device",
        "token_uri": "https://example.com/token",
        "client_id_env": "EXAMPLE_OAUTH_CLIENT",
    })
    desc = parse_service_descriptor(raw)
    with pytest.raises(ServiceDescriptorError, match="not set"):
        desc.resolve_client_id()


def test_resolve_client_id_raises_for_api_token_service():
    desc = parse_service_descriptor(_api_token_descriptor())
    with pytest.raises(ServiceDescriptorError, match="no oauth config"):
        desc.resolve_client_id()


# ---------------------------------------------------------------------------
# Back-compat: existing api_token descriptors parse unchanged
# ---------------------------------------------------------------------------


def test_api_token_descriptor_still_parses_without_oauth_section():
    desc = parse_service_descriptor(_api_token_descriptor())
    assert desc.oauth is None
    assert desc.auth_type == AuthType.API_TOKEN
