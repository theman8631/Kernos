"""Tests for Telegram adapter and poller."""
import pytest
from datetime import datetime, timezone

from kernos.messages.adapters.telegram_bot import TelegramAdapter, TELEGRAM_MAX_LENGTH
from kernos.messages.models import AuthLevel


def _make_update(
    user_id: int = 12345,
    chat_id: int = 67890,
    text: str = "Hello",
    date: int = 1712000000,
) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": chat_id, "type": "private"},
            "date": date,
            "text": text,
        },
    }


class TestTelegramAdapter:
    def test_inbound_basic(self):
        adapter = TelegramAdapter()
        msg = adapter.inbound(_make_update(user_id=111, chat_id=222, text="Hi there"))
        assert msg.content == "Hi there"
        assert msg.sender == "111"
        assert msg.conversation_id == "222"
        assert msg.platform == "telegram"
        assert msg.sender_auth_level == AuthLevel.unknown

    def test_inbound_empty_text(self):
        update = _make_update(text="")
        adapter = TelegramAdapter()
        msg = adapter.inbound(update)
        assert msg.content == ""

    def test_outbound_passthrough(self):
        adapter = TelegramAdapter()
        assert adapter.outbound("hello", None) == "hello"

    def test_chunk_short_message(self):
        chunks = TelegramAdapter._chunk("short message")
        assert len(chunks) == 1
        assert chunks[0] == "short message"

    def test_chunk_long_message(self):
        long_text = "Line\n" * 2000  # ~10000 chars
        chunks = TelegramAdapter._chunk(long_text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= TELEGRAM_MAX_LENGTH

    def test_chunk_no_newlines(self):
        long_text = "x" * 5000
        chunks = TelegramAdapter._chunk(long_text)
        assert len(chunks) == 2
        assert len(chunks[0]) == TELEGRAM_MAX_LENGTH
        assert len(chunks[1]) == 5000 - TELEGRAM_MAX_LENGTH

    def test_can_send_outbound_requires_token(self):
        adapter = TelegramAdapter()
        adapter._bot_token = ""
        assert adapter.can_send_outbound is False
        adapter._bot_token = "test_token"
        assert adapter.can_send_outbound is True


class TestTelegramPoller:
    def test_poller_init(self):
        from kernos.telegram_poller import TelegramPoller
        from unittest.mock import MagicMock
        poller = TelegramPoller(
            adapter=MagicMock(),
            handler=MagicMock(),
            bot_token="test:token",
        )
        assert poller._base_url == "https://api.telegram.org/bottest:token"
        assert poller._last_update_id == 0
        assert poller._poll_timeout == 30
