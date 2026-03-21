"""Tests for SPEC-3E-A: Outbound Messaging + Channels.

Covers: ChannelRegistry, manage_channels tool, BaseAdapter send_outbound,
member_id resolution, NormalizedMessage.member_id field.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.channels import (
    MANAGE_CHANNELS_TOOL,
    ChannelInfo,
    ChannelRegistry,
    handle_manage_channels,
)
from kernos.messages.adapters.base import BaseAdapter
from kernos.messages.models import NormalizedMessage, AuthLevel


# ---------------------------------------------------------------------------
# ChannelInfo and ChannelRegistry
# ---------------------------------------------------------------------------


class TestChannelRegistry:
    def test_register_and_get(self):
        reg = ChannelRegistry()
        ch = ChannelInfo(
            name="discord", display_name="Discord", status="connected",
            source="default", can_send_outbound=True, channel_target="123",
            platform="discord",
        )
        reg.register(ch)
        assert reg.get("discord") is ch

    def test_get_all(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("a", "A", "connected", "default", True, "", "a"))
        reg.register(ChannelInfo("b", "B", "disabled", "default", False, "", "b"))
        assert len(reg.get_all()) == 2

    def test_get_connected(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("a", "A", "connected", "default", True, "", "a"))
        reg.register(ChannelInfo("b", "B", "disabled", "default", False, "", "b"))
        connected = reg.get_connected()
        assert len(connected) == 1
        assert connected[0].name == "a"

    def test_get_outbound_capable(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        reg.register(ChannelInfo("cli", "CLI", "connected", "default", False, "", "cli"))
        capable = reg.get_outbound_capable()
        assert len(capable) == 1
        assert capable[0].name == "discord"

    def test_disable_and_enable(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "", "discord"))

        assert reg.disable("discord")
        assert reg.get("discord").status == "disabled"
        assert len(reg.get_connected()) == 0

        assert reg.enable("discord")
        assert reg.get("discord").status == "connected"

    def test_disable_nonexistent(self):
        reg = ChannelRegistry()
        assert not reg.disable("nope")

    def test_update_target(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "", "discord"))
        reg.update_target("discord", "channel_456")
        assert reg.get("discord").channel_target == "channel_456"


# ---------------------------------------------------------------------------
# manage_channels tool
# ---------------------------------------------------------------------------


class TestManageChannelsTool:
    def test_tool_shape(self):
        assert MANAGE_CHANNELS_TOOL["name"] == "manage_channels"
        schema = MANAGE_CHANNELS_TOOL["input_schema"]
        assert "action" in schema["properties"]
        assert set(schema["properties"]["action"]["enum"]) == {"list", "enable", "disable"}

    def test_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "manage_channels" in ReasoningService._KERNEL_TOOLS


class TestManageChannelsHandler:
    def test_list(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        reg.register(ChannelInfo("cli", "CLI", "connected", "default", False, "", "cli"))

        result = handle_manage_channels(reg, "list")
        assert "Discord" in result
        assert "CLI" in result
        assert "can push" in result
        assert "receive only" in result

    def test_list_empty(self):
        reg = ChannelRegistry()
        result = handle_manage_channels(reg, "list")
        assert "No communication channels" in result

    def test_enable(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "disabled", "default", True, "", "discord"))
        result = handle_manage_channels(reg, "enable", "discord")
        assert "Enabled" in result

    def test_disable(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "", "discord"))
        result = handle_manage_channels(reg, "disable", "discord")
        assert "Disabled" in result

    def test_enable_without_channel(self):
        reg = ChannelRegistry()
        result = handle_manage_channels(reg, "enable")
        assert "Error" in result

    def test_enable_nonexistent(self):
        reg = ChannelRegistry()
        result = handle_manage_channels(reg, "enable", "nope")
        assert "not found" in result


# ---------------------------------------------------------------------------
# BaseAdapter defaults
# ---------------------------------------------------------------------------


class _StubAdapter(BaseAdapter):
    """Minimal concrete adapter for testing base class defaults."""
    def inbound(self, raw_request):
        raise NotImplementedError
    def outbound(self, response, original_message):
        raise NotImplementedError


class TestBaseAdapterDefaults:
    def test_default_cannot_send_outbound(self):
        adapter = _StubAdapter()
        assert not adapter.can_send_outbound

    async def test_default_send_outbound_returns_false(self):
        adapter = _StubAdapter()
        result = await adapter.send_outbound("t1", "target", "msg")
        assert result is False


# ---------------------------------------------------------------------------
# NormalizedMessage.member_id
# ---------------------------------------------------------------------------


class TestMemberIdField:
    def test_default_empty(self):
        from datetime import datetime, timezone
        msg = NormalizedMessage(
            content="hi", sender="123", sender_auth_level=AuthLevel.owner_verified,
            platform="discord", platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc),
            tenant_id="t1",
        )
        assert msg.member_id == ""

    def test_set_member_id(self):
        from datetime import datetime, timezone
        msg = NormalizedMessage(
            content="hi", sender="123", sender_auth_level=AuthLevel.owner_verified,
            platform="discord", platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc),
            tenant_id="t1", member_id="member:t1:owner",
        )
        assert msg.member_id == "member:t1:owner"


# ---------------------------------------------------------------------------
# _resolve_member
# ---------------------------------------------------------------------------


class TestResolveMember:
    def test_single_member_returns_owner(self):
        from kernos.messages.handler import MessageHandler
        handler = MagicMock(spec=MessageHandler)
        handler._resolve_member = MessageHandler._resolve_member.__get__(handler)
        result = handler._resolve_member("discord:123", "discord", "123")
        assert result == "member:discord:123:owner"

    def test_different_platform_same_tenant(self):
        from kernos.messages.handler import MessageHandler
        handler = MagicMock(spec=MessageHandler)
        handler._resolve_member = MessageHandler._resolve_member.__get__(handler)
        result = handler._resolve_member("discord:123", "sms", "+15551234567")
        assert result == "member:discord:123:owner"


# ---------------------------------------------------------------------------
# TwilioSMSAdapter authorization
# ---------------------------------------------------------------------------


class TestTwilioAuthorization:
    def test_authorized_number(self):
        import os
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("AUTHORIZED_NUMBERS", "+15551234567,+15559876543")
            mp.setenv("OWNER_PHONE_NUMBER", "+15551234567")
            mp.setenv("TWILIO_ACCOUNT_SID", "")
            mp.setenv("TWILIO_AUTH_TOKEN", "")
            mp.setenv("TWILIO_PHONE_NUMBER", "")
            from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
            adapter = TwilioSMSAdapter()
            assert adapter.is_authorized("+15551234567")
            assert adapter.is_authorized("+15559876543")

    def test_unauthorized_number(self):
        import os
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("AUTHORIZED_NUMBERS", "+15551234567")
            mp.setenv("OWNER_PHONE_NUMBER", "+15551234567")
            mp.setenv("TWILIO_ACCOUNT_SID", "")
            mp.setenv("TWILIO_AUTH_TOKEN", "")
            mp.setenv("TWILIO_PHONE_NUMBER", "")
            from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
            adapter = TwilioSMSAdapter()
            assert not adapter.is_authorized("+19999999999")

    def test_owner_always_authorized(self):
        import os
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("AUTHORIZED_NUMBERS", "")
            mp.setenv("OWNER_PHONE_NUMBER", "+15551234567")
            mp.setenv("TWILIO_ACCOUNT_SID", "")
            mp.setenv("TWILIO_AUTH_TOKEN", "")
            mp.setenv("TWILIO_PHONE_NUMBER", "")
            from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
            adapter = TwilioSMSAdapter()
            assert adapter.is_authorized("+15551234567")
