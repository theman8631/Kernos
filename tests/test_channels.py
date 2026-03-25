"""Tests for SPEC-3E-A: Outbound Messaging + Channels + Cross-Channel.

Covers: ChannelRegistry, manage_channels tool, BaseAdapter send_outbound,
member_id resolution, NormalizedMessage.member_id field,
resolve_channel_alias, send_to_channel tool, system prompt channel block.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.channels import (
    MANAGE_CHANNELS_TOOL,
    SEND_TO_CHANNEL_TOOL,
    ChannelInfo,
    ChannelRegistry,
    handle_manage_channels,
    resolve_channel_alias,
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


# ---------------------------------------------------------------------------
# Channel alias resolver
# ---------------------------------------------------------------------------


class TestResolveChannelAlias:
    def test_sms_aliases(self):
        assert resolve_channel_alias("sms") == "sms"
        assert resolve_channel_alias("text") == "sms"
        assert resolve_channel_alias("phone") == "sms"
        assert resolve_channel_alias("my phone") == "sms"
        assert resolve_channel_alias("txt") == "sms"

    def test_discord_aliases(self):
        assert resolve_channel_alias("discord") == "discord"
        assert resolve_channel_alias("chat") == "discord"
        assert resolve_channel_alias("over chat") == "discord"

    def test_email_aliases(self):
        assert resolve_channel_alias("email") == "email"
        assert resolve_channel_alias("gmail") == "email"
        assert resolve_channel_alias("mail") == "email"

    def test_case_insensitive(self):
        assert resolve_channel_alias("SMS") == "sms"
        assert resolve_channel_alias("Discord") == "discord"
        assert resolve_channel_alias("TEXT") == "sms"

    def test_strips_whitespace(self):
        assert resolve_channel_alias("  sms  ") == "sms"
        assert resolve_channel_alias(" discord ") == "discord"

    def test_unknown_returns_lowered(self):
        assert resolve_channel_alias("slack") == "slack"
        assert resolve_channel_alias("TEAMS") == "teams"

    def test_deterministic(self):
        """Alias resolver is deterministic — no LLM call (AC7)."""
        for _ in range(100):
            assert resolve_channel_alias("text") == "sms"


# ---------------------------------------------------------------------------
# send_to_channel tool definition
# ---------------------------------------------------------------------------


class TestSendToChannelTool:
    def test_tool_shape(self):
        assert SEND_TO_CHANNEL_TOOL["name"] == "send_to_channel"
        schema = SEND_TO_CHANNEL_TOOL["input_schema"]
        assert "channel" in schema["properties"]
        assert "message" in schema["properties"]
        assert set(schema["required"]) == {"channel", "message"}

    def test_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "send_to_channel" in ReasoningService._KERNEL_TOOLS

    def test_classified_as_write(self):
        """send_to_channel is a write operation (AC9)."""
        from kernos.kernel.reasoning import ReasoningService
        svc = MagicMock(spec=ReasoningService)
        svc._registry = None
        svc._classify_tool_effect = ReasoningService._classify_tool_effect.__get__(svc)
        assert svc._classify_tool_effect("send_to_channel", None) == "soft_write"


# ---------------------------------------------------------------------------
# send_to_channel dispatch (via execute_tool)
# ---------------------------------------------------------------------------


class TestSendToChannelDispatch:
    def _make_reasoning_svc(self):
        """Create a minimal ReasoningService mock with channel registry and handler."""
        from kernos.kernel.reasoning import ReasoningService
        svc = MagicMock(spec=ReasoningService)
        svc._KERNEL_TOOLS = ReasoningService._KERNEL_TOOLS
        svc.execute_tool = ReasoningService.execute_tool.__get__(svc)

        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        reg.register(ChannelInfo("sms", "Twilio SMS", "connected", "default", True, "+1555", "sms"))
        reg.register(ChannelInfo("cli", "CLI Terminal", "connected", "default", False, "", "cli"))
        svc._channel_registry = reg

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        svc._handler = handler
        svc._mcp = MagicMock()
        return svc, handler

    async def test_send_to_sms(self):
        svc, handler = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "sms", "message": "hello"},
            request,
        )
        assert "Twilio SMS" in result
        handler.send_outbound.assert_called_once_with("t1", "member:t1:owner", "sms", "hello")

    async def test_send_to_sms_via_alias_member_id(self):
        """Verify member_id is derived from tenant_id."""
        svc, handler = self._make_reasoning_svc()
        request = MagicMock(tenant_id="discord:999", active_space_id="space1")
        await svc.execute_tool(
            "send_to_channel", {"channel": "sms", "message": "hi"},
            request,
        )
        handler.send_outbound.assert_called_once_with("discord:999", "member:discord:999:owner", "sms", "hi")

    async def test_send_resolves_alias(self):
        """AC4: 'text' resolves to 'sms'."""
        svc, handler = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "text", "message": "hi"},
            request,
        )
        assert "Twilio SMS" in result
        handler.send_outbound.assert_called_once_with("t1", "member:t1:owner", "sms", "hi")  # resolved alias

    async def test_send_to_discord(self):
        """AC5."""
        svc, handler = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "discord", "message": "report"},
            request,
        )
        assert "Discord" in result

    async def test_invalid_channel_error(self):
        """AC6: clear error for unregistered channel."""
        svc, _ = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "email", "message": "test"},
            request,
        )
        assert "not registered" in result
        assert "discord" in result  # lists available channels

    async def test_non_outbound_channel_error(self):
        """AC6: clear error for receive-only channel."""
        svc, _ = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "cli", "message": "test"},
            request,
        )
        assert "cannot send outbound" in result

    async def test_disconnected_channel_error(self):
        """AC6: clear error for disconnected channel."""
        svc, _ = self._make_reasoning_svc()
        svc._channel_registry.disable("sms")
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "sms", "message": "test"},
            request,
        )
        assert "not connected" in result

    async def test_missing_params_error(self):
        svc, _ = self._make_reasoning_svc()
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "", "message": ""},
            request,
        )
        assert "required" in result

    async def test_send_failure_returns_error(self):
        svc, handler = self._make_reasoning_svc()
        handler.send_outbound = AsyncMock(side_effect=RuntimeError("network down"))
        request = MagicMock(tenant_id="t1", active_space_id="space1")
        result = await svc.execute_tool(
            "send_to_channel", {"channel": "sms", "message": "test"},
            request,
        )
        assert "Failed to send" in result


# ---------------------------------------------------------------------------
# System prompt channel block
# ---------------------------------------------------------------------------


class TestSystemPromptChannelBlock:
    def _make_prompt(self, platform: str = "discord", registry: ChannelRegistry | None = None):
        from kernos.messages.handler import _build_system_prompt, PRIMARY_TEMPLATE
        from kernos.kernel.state import Soul
        msg = NormalizedMessage(
            content="hi", sender="123", sender_auth_level=AuthLevel.owner_verified,
            platform=platform, platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc),
            tenant_id="t1",
        )
        soul = Soul(tenant_id="t1")
        return _build_system_prompt(
            msg, "CAPABILITIES", soul, PRIMARY_TEMPLATE, [],
            channel_registry=registry,
        )

    def test_channel_block_present_with_registry(self):
        """AC1: system prompt includes available outbound channels."""
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        reg.register(ChannelInfo("sms", "Twilio SMS", "connected", "default", True, "+1555", "sms"))
        prompt = self._make_prompt("discord", reg)
        assert "OUTBOUND CHANNELS" in prompt
        assert "send_to_channel" in prompt

    def test_current_channel_marker(self):
        """AC2: current channel marked with (current)."""
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        reg.register(ChannelInfo("sms", "Twilio SMS", "connected", "default", True, "+1555", "sms"))
        prompt = self._make_prompt("discord", reg)
        assert "discord: Discord [can send] (current)" in prompt
        assert "(current)" not in prompt.split("sms: Twilio SMS")[1].split("\n")[0]

    def test_single_channel_still_shown(self):
        """AC1: always show, even with one channel."""
        reg = ChannelRegistry()
        reg.register(ChannelInfo("discord", "Discord", "connected", "default", True, "123", "discord"))
        prompt = self._make_prompt("discord", reg)
        assert "OUTBOUND CHANNELS" in prompt

    def test_no_registry_no_block(self):
        prompt = self._make_prompt("discord", None)
        assert "OUTBOUND CHANNELS" not in prompt

    def test_receive_only_shown(self):
        reg = ChannelRegistry()
        reg.register(ChannelInfo("cli", "CLI", "connected", "default", False, "", "cli"))
        prompt = self._make_prompt("cli", reg)
        assert "receive only" in prompt


# ---------------------------------------------------------------------------
# manage_channels unchanged (AC11)
# ---------------------------------------------------------------------------


class TestManageChannelsUnchanged:
    def test_no_send_action(self):
        """AC11: manage_channels only has list/enable/disable — no send."""
        schema = MANAGE_CHANNELS_TOOL["input_schema"]
        assert set(schema["properties"]["action"]["enum"]) == {"list", "enable", "disable"}
