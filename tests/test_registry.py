"""Tests for CapabilityRegistry and related data model."""
from unittest.mock import MagicMock

import pytest

from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import (
    CapabilityInfo,
    CapabilityRegistry,
    CapabilityStatus,
)


def _calendar_cap(**kwargs) -> CapabilityInfo:
    defaults = dict(
        name="google-calendar",
        display_name="Google Calendar",
        description="Check your schedule, list events, find availability.",
        category="calendar",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Google Calendar.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="google-calendar",
    )
    defaults.update(kwargs)
    return CapabilityInfo(**defaults)


def _gmail_cap(**kwargs) -> CapabilityInfo:
    defaults = dict(
        name="gmail",
        display_name="Gmail",
        description="Read, categorize, and draft email responses",
        category="email",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Gmail.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="gmail",
    )
    defaults.update(kwargs)
    return CapabilityInfo(**defaults)


# ---------------------------------------------------------------------------
# CapabilityInfo
# ---------------------------------------------------------------------------


def test_capability_info_all_fields():
    cap = CapabilityInfo(
        name="test-cap",
        display_name="Test Cap",
        description="A test capability",
        category="test",
        status=CapabilityStatus.AVAILABLE,
        tools=["tool1", "tool2"],
        setup_hint="Set it up like this.",
        setup_requires=["ENV_VAR"],
        server_name="test-server",
        error_message="",
    )
    assert cap.name == "test-cap"
    assert cap.display_name == "Test Cap"
    assert cap.description == "A test capability"
    assert cap.category == "test"
    assert cap.status == CapabilityStatus.AVAILABLE
    assert cap.tools == ["tool1", "tool2"]
    assert cap.setup_hint == "Set it up like this."
    assert cap.setup_requires == ["ENV_VAR"]
    assert cap.server_name == "test-server"
    assert cap.error_message == ""


# ---------------------------------------------------------------------------
# CapabilityRegistry — CRUD
# ---------------------------------------------------------------------------


def test_registry_register_and_get():
    reg = CapabilityRegistry()
    cap = _calendar_cap()
    reg.register(cap)
    result = reg.get("google-calendar")
    assert result is not None
    assert result.name == "google-calendar"


def test_registry_get_returns_none_for_unknown():
    reg = CapabilityRegistry()
    assert reg.get("nonexistent") is None


def test_registry_register_stores_independent_copy():
    """Mutating the original cap after registration must not affect the registry."""
    reg = CapabilityRegistry()
    cap = _calendar_cap()
    reg.register(cap)
    cap.status = CapabilityStatus.CONNECTED  # mutate original
    stored = reg.get("google-calendar")
    assert stored.status == CapabilityStatus.AVAILABLE  # registry copy unaffected


def test_registry_get_all():
    reg = CapabilityRegistry()
    reg.register(_calendar_cap())
    reg.register(_gmail_cap())
    all_caps = reg.get_all()
    assert len(all_caps) == 2
    names = {c.name for c in all_caps}
    assert names == {"google-calendar", "gmail"}


# ---------------------------------------------------------------------------
# CapabilityRegistry — filtering
# ---------------------------------------------------------------------------


def test_get_connected_returns_only_connected():
    reg = CapabilityRegistry()
    reg.register(_calendar_cap(status=CapabilityStatus.CONNECTED))
    reg.register(_gmail_cap(status=CapabilityStatus.AVAILABLE))
    connected = reg.get_connected()
    assert len(connected) == 1
    assert connected[0].name == "google-calendar"


def test_get_available_returns_only_available():
    reg = CapabilityRegistry()
    reg.register(_calendar_cap(status=CapabilityStatus.CONNECTED))
    reg.register(_gmail_cap(status=CapabilityStatus.AVAILABLE))
    available = reg.get_available()
    assert len(available) == 1
    assert available[0].name == "gmail"


def test_get_by_category():
    reg = CapabilityRegistry()
    reg.register(_calendar_cap(category="calendar"))
    reg.register(_gmail_cap(category="email"))
    reg.register(CapabilityInfo(
        name="web-search", display_name="Web Search",
        description="Search the internet", category="search",
        status=CapabilityStatus.AVAILABLE,
    ))
    assert len(reg.get_by_category("calendar")) == 1
    assert len(reg.get_by_category("email")) == 1
    assert len(reg.get_by_category("search")) == 1
    assert len(reg.get_by_category("nonexistent")) == 0


# ---------------------------------------------------------------------------
# get_connected_tools
# ---------------------------------------------------------------------------


def test_get_connected_tools_delegates_to_mcp():
    mock_mcp = MagicMock()
    mock_mcp.get_tools.return_value = [{"name": "get-events", "description": "List events"}]
    reg = CapabilityRegistry(mcp=mock_mcp)
    tools = reg.get_connected_tools()
    assert tools == [{"name": "get-events", "description": "List events"}]
    mock_mcp.get_tools.assert_called_once()


def test_get_connected_tools_returns_empty_without_mcp():
    reg = CapabilityRegistry(mcp=None)
    assert reg.get_connected_tools() == []


# ---------------------------------------------------------------------------
# build_capability_prompt
# ---------------------------------------------------------------------------


def test_build_capability_prompt_no_capabilities():
    reg = CapabilityRegistry()
    prompt = reg.build_capability_prompt()
    assert "conversation" in prompt.lower()
    assert "CONNECTED" not in prompt
    assert "AVAILABLE" not in prompt


def test_build_capability_prompt_with_connected():
    from kernos.kernel.spaces import ContextSpace
    reg = CapabilityRegistry()
    reg.register(_calendar_cap(status=CapabilityStatus.CONNECTED))
    system_space = ContextSpace(id="sys", tenant_id="t1", name="System", space_type="system")
    prompt = reg.build_capability_prompt(space=system_space)
    assert "CONNECTED CAPABILITIES" in prompt
    assert "Google Calendar" in prompt
    assert "Check your schedule" in prompt


def test_build_capability_prompt_with_available():
    reg = CapabilityRegistry()
    reg.register(_gmail_cap(status=CapabilityStatus.AVAILABLE))
    prompt = reg.build_capability_prompt()
    assert "AVAILABLE CAPABILITIES" in prompt
    assert "Gmail" in prompt
    assert "I can connect to your Gmail." in prompt


def test_build_capability_prompt_includes_both_tiers():
    from kernos.kernel.spaces import ContextSpace
    reg = CapabilityRegistry()
    reg.register(_calendar_cap(status=CapabilityStatus.CONNECTED))
    reg.register(_gmail_cap(status=CapabilityStatus.AVAILABLE))
    system_space = ContextSpace(id="sys", tenant_id="t1", name="System", space_type="system")
    prompt = reg.build_capability_prompt(space=system_space)
    assert "CONNECTED CAPABILITIES" in prompt
    assert "AVAILABLE CAPABILITIES" in prompt
    assert "Google Calendar" in prompt
    assert "Gmail" in prompt


def test_build_capability_prompt_no_connected_shows_conversation_plus_available():
    reg = CapabilityRegistry()
    reg.register(_gmail_cap(status=CapabilityStatus.AVAILABLE))
    prompt = reg.build_capability_prompt()
    assert "conversation" in prompt.lower()
    assert "AVAILABLE CAPABILITIES" in prompt
    assert "Gmail" in prompt


# ---------------------------------------------------------------------------
# KNOWN_CAPABILITIES catalog sanity check
# ---------------------------------------------------------------------------


def test_known_capabilities_has_three_entries():
    assert len(KNOWN_CAPABILITIES) >= 3


def test_known_capabilities_names():
    names = {c.name for c in KNOWN_CAPABILITIES}
    assert "google-calendar" in names
    assert "gmail" in names
    assert "web-search" in names


def test_known_capabilities_all_available_by_default():
    for cap in KNOWN_CAPABILITIES:
        assert cap.status == CapabilityStatus.AVAILABLE, (
            f"{cap.name} should default to AVAILABLE, got {cap.status}"
        )
