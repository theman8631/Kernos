import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.models import AuthLevel

OWNER_DISCORD_ID = "123456789"
OTHER_DISCORD_ID = "987654321"
OWNER_PHONE = "+15555550100"


def make_mock_message(
    author_id: int = int(OWNER_DISCORD_ID),
    content: str = "Hello Kernos",
    channel_id: int = 111222333,
    channel_name: str = "general",
    guild_id: int = 999888777,
    in_guild: bool = True,
) -> MagicMock:
    """Build a mock discord.Message for testing."""
    msg = MagicMock()
    msg.content = content
    msg.author.id = author_id
    msg.author.bot = False
    msg.channel.id = channel_id
    msg.channel.name = channel_name
    msg.created_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    if in_guild:
        msg.guild = MagicMock()
        msg.guild.id = guild_id
    else:
        msg.guild = None
    return msg


@pytest.fixture
def adapter():
    with patch.dict(
        os.environ,
        {"DISCORD_OWNER_ID": OWNER_DISCORD_ID, "OWNER_PHONE_NUMBER": OWNER_PHONE},
    ):
        return DiscordAdapter()


# --- Inbound: core fields ---


def test_inbound_platform_is_discord(adapter):
    nm = adapter.inbound(make_mock_message())
    assert nm.platform == "discord"


def test_inbound_capabilities(adapter):
    nm = adapter.inbound(make_mock_message())
    assert "text" in nm.platform_capabilities
    assert "embeds" in nm.platform_capabilities
    assert "attachments" in nm.platform_capabilities
    assert "reactions" in nm.platform_capabilities


def test_inbound_content(adapter):
    nm = adapter.inbound(make_mock_message(content="What time is it?"))
    assert nm.content == "What time is it?"


def test_inbound_sender_is_string_author_id(adapter):
    nm = adapter.inbound(make_mock_message(author_id=int(OWNER_DISCORD_ID)))
    assert nm.sender == OWNER_DISCORD_ID


def test_inbound_conversation_id_is_channel_id(adapter):
    nm = adapter.inbound(make_mock_message(channel_id=555666777))
    assert nm.conversation_id == "555666777"


def test_inbound_timestamp(adapter):
    nm = adapter.inbound(make_mock_message())
    assert nm.timestamp == datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Auth level ---


def test_owner_gets_verified(adapter):
    nm = adapter.inbound(make_mock_message(author_id=int(OWNER_DISCORD_ID)))
    assert nm.sender_auth_level == AuthLevel.owner_verified


def test_non_owner_gets_unknown(adapter):
    nm = adapter.inbound(make_mock_message(author_id=int(OTHER_DISCORD_ID)))
    assert nm.sender_auth_level == AuthLevel.unknown


def test_missing_owner_id_env_defaults_to_unknown():
    """If DISCORD_OWNER_ID is unset, every sender is unknown (safe default)."""
    with patch.dict(
        os.environ,
        {"OWNER_PHONE_NUMBER": OWNER_PHONE},
        clear=False,
    ):
        # Ensure DISCORD_OWNER_ID is absent
        os.environ.pop("DISCORD_OWNER_ID", None)
        a = DiscordAdapter()
    nm = a.inbound(make_mock_message(author_id=int(OWNER_DISCORD_ID)))
    assert nm.sender_auth_level == AuthLevel.unknown


# --- Tenant ID ---


def test_tenant_id_is_owner_phone(adapter):
    nm = adapter.inbound(make_mock_message())
    assert nm.tenant_id == OWNER_PHONE


def test_non_owner_tenant_id_is_still_owner_phone(adapter):
    """Even non-owner senders resolve to the single owner tenant in Phase 1A."""
    nm = adapter.inbound(make_mock_message(author_id=int(OTHER_DISCORD_ID)))
    assert nm.tenant_id == OWNER_PHONE


# --- Guild context ---


def test_guild_context_included_in_server(adapter):
    nm = adapter.inbound(
        make_mock_message(guild_id=999888777, channel_name="general", in_guild=True)
    )
    assert nm.context is not None
    assert nm.context["guild_id"] == "999888777"
    assert nm.context["channel_name"] == "general"


def test_dm_context_is_none(adapter):
    nm = adapter.inbound(make_mock_message(in_guild=False))
    assert nm.context is None


# --- Outbound ---


def test_outbound_returns_response_unchanged(adapter):
    nm = adapter.inbound(make_mock_message())
    result = adapter.outbound("Hello from Kernos!", nm)
    assert result == "Hello from Kernos!"


def test_outbound_empty_string(adapter):
    nm = adapter.inbound(make_mock_message())
    result = adapter.outbound("", nm)
    assert result == ""


def test_outbound_long_response_unchanged(adapter):
    """Discord allows 2000 chars — adapter does no truncation."""
    long_response = "x" * 1999
    nm = adapter.inbound(make_mock_message())
    result = adapter.outbound(long_response, nm)
    assert result == long_response


# --- Architectural constraint: no handler imports ---


def test_no_handler_imports_in_discord_adapter():
    """The discord adapter must not import from kernos.messages.handler."""
    import kernos.messages.adapters.discord_bot as module

    assert module.__file__ is not None
    with open(module.__file__) as f:
        source = f.read()
    assert "kernos.messages.handler" not in source
    assert "import handler" not in source


# --- Response chunking ---


class TestChunkResponse:
    def test_short_message_single_chunk(self):
        from kernos.server import _chunk_response
        result = _chunk_response("Hello world")
        assert result == ["Hello world"]

    def test_exact_limit_single_chunk(self):
        from kernos.server import _chunk_response, DISCORD_MAX_LENGTH
        text = "x" * DISCORD_MAX_LENGTH
        result = _chunk_response(text)
        assert result == [text]

    def test_splits_on_newline(self):
        from kernos.server import _chunk_response, DISCORD_MAX_LENGTH
        # Two lines that fit in one chunk, third forces a second chunk
        line = "a" * 900
        text = line + "\n" + line + "\n" + line
        result = _chunk_response(text)
        assert all(len(c) <= DISCORD_MAX_LENGTH for c in result)
        assert len(result) == 2

    def test_hard_cut_when_no_newline(self):
        from kernos.server import _chunk_response, DISCORD_MAX_LENGTH
        text = "x" * 4500  # No newlines at all
        result = _chunk_response(text)
        assert all(len(c) <= DISCORD_MAX_LENGTH for c in result)
        assert "".join(result) == text

    def test_empty_string(self):
        from kernos.server import _chunk_response
        result = _chunk_response("")
        assert result == [""]

    def test_many_short_lines(self):
        from kernos.server import _chunk_response, DISCORD_MAX_LENGTH
        text = "\n".join(f"Line {i}" for i in range(500))
        result = _chunk_response(text)
        assert all(len(c) <= DISCORD_MAX_LENGTH for c in result)
        assert len(result) > 1
