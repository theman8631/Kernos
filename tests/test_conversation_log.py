"""Tests for SPEC-CONVERSATION-LOGS-P1: Per-space log files."""
import json
from pathlib import Path

import pytest

from kernos.kernel.conversation_log import ConversationLogger


class TestConversationLoggerAppend:
    async def test_creates_log_file(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello world")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        assert log.exists()
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        assert "[user]" in lines[0]
        assert "[discord]" in lines[0]
        assert "Hello world" in lines[0]

    async def test_appends_multiple_lines(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello")
        await logger.append("t1", "space_abc", "assistant", "discord", "Hi there!")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 2
        assert "[user]" in lines[0]
        assert "[assistant]" in lines[1]

    async def test_cross_channel_same_log(self, tmp_path):
        """Discord and SMS messages land in the same log for the same space."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "From Discord")
        await logger.append("t1", "space_abc", "user", "sms", "From SMS")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 2
        assert "[discord]" in lines[0]
        assert "[sms]" in lines[1]

    async def test_scheduled_channel(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "assistant", "scheduled", "Reminder: dentist")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        content = log.read_text()
        assert "[scheduled]" in content
        assert "dentist" in content

    async def test_whisper_channel(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "assistant", "whisper", "Meeting in 30 min")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        content = log.read_text()
        assert "[whisper]" in content


class TestNewlineEscaping:
    async def test_multiline_escaped(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Line 1\nLine 2\nLine 3")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        lines = log.read_text().strip().split("\n")
        # Should be exactly one log line (newlines escaped)
        assert len(lines) == 1
        assert "\\n" in lines[0]
        assert "Line 1\\nLine 2\\nLine 3" in lines[0]


class TestMetaFile:
    async def test_meta_created(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello")

        meta_path = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["current_log"] == 1
        assert meta["current_log_tokens_est"] > 0
        assert "created_at" in meta

    async def test_token_estimate_increases(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Short")
        meta1 = logger._load_meta("t1", "space_abc")
        est1 = meta1["current_log_tokens_est"]

        await logger.append("t1", "space_abc", "assistant", "discord", "A much longer response with more content")
        meta2 = logger._load_meta("t1", "space_abc")
        assert meta2["current_log_tokens_est"] > est1


class TestTimestamp:
    async def test_custom_timestamp(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello",
                           timestamp="2026-03-22T14:00:06-07:00")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        content = log.read_text()
        assert "[2026-03-22T14:00:06-07:00]" in content

    async def test_default_timestamp(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        content = log.read_text()
        # Should have a timestamp in brackets
        assert content.startswith("[2026-")


class TestLogFormat:
    async def test_format_matches_spec(self, tmp_path):
        """Verify exact format: [{timestamp}] [{speaker}] [{channel}] {content}"""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Test message",
                           timestamp="2026-03-22T14:00:06-07:00")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        line = log.read_text().strip()
        assert line == "[2026-03-22T14:00:06-07:00] [user] [discord] Test message"


class TestEmptySpaceId:
    async def test_skips_empty_space(self, tmp_path):
        """Should not create files for empty space_id."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "", "user", "discord", "Hello")

        tenants_dir = tmp_path / "tenants"
        assert not tenants_dir.exists()


# ---------------------------------------------------------------------------
# P2: Read — read_recent + parse
# ---------------------------------------------------------------------------


class TestReadRecent:
    async def test_reads_entries_in_order(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "First")
        await logger.append("t1", "s1", "assistant", "discord", "Second")
        await logger.append("t1", "s1", "user", "sms", "Third")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 3
        assert entries[0]["content"] == "First"
        assert entries[0]["role"] == "user"
        assert entries[0]["channel"] == "discord"
        assert entries[1]["content"] == "Second"
        assert entries[1]["role"] == "assistant"
        assert entries[2]["content"] == "Third"
        assert entries[2]["channel"] == "sms"

    async def test_cross_channel_in_same_log(self, tmp_path):
        """Discord and SMS messages appear in the same read."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "From Discord")
        await logger.append("t1", "s1", "assistant", "discord", "Reply on Discord")
        await logger.append("t1", "s1", "user", "sms", "From SMS")

        entries = await logger.read_recent("t1", "s1")
        channels = [e["channel"] for e in entries]
        assert "discord" in channels
        assert "sms" in channels

    async def test_empty_log_returns_empty(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        entries = await logger.read_recent("t1", "s1")
        assert entries == []

    async def test_empty_space_returns_empty(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        entries = await logger.read_recent("t1", "")
        assert entries == []

    async def test_max_messages_cap(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        for i in range(10):
            await logger.append("t1", "s1", "user", "discord", f"Msg {i}")

        entries = await logger.read_recent("t1", "s1", max_messages=3, token_budget=100000)
        assert len(entries) == 3
        # Should be the LAST 3 messages (most recent)
        assert entries[0]["content"] == "Msg 7"
        assert entries[2]["content"] == "Msg 9"

    async def test_token_budget_limits_entries(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        # Write a short message and a very long one
        await logger.append("t1", "s1", "user", "discord", "Short")
        await logger.append("t1", "s1", "assistant", "discord", "A" * 2000)  # ~500 tokens
        await logger.append("t1", "s1", "user", "discord", "After long")

        # Budget of 200 tokens — should get last 1-2 messages, not all 3
        entries = await logger.read_recent("t1", "s1", token_budget=200)
        assert len(entries) < 3
        # Should always include at least one entry
        assert len(entries) >= 1

    async def test_multiline_unescaped(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Line 1\nLine 2\nLine 3")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "Line 1\nLine 2\nLine 3"

    async def test_scheduled_and_whisper_channels(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "assistant", "scheduled", "Reminder!")
        await logger.append("t1", "s1", "assistant", "whisper", "Meeting in 30 min")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 2
        assert entries[0]["channel"] == "scheduled"
        assert entries[1]["channel"] == "whisper"


class TestParseLogLine:
    def test_parses_standard_line(self):
        logger = ConversationLogger()
        result = logger._parse_log_line(
            "[2026-03-22T14:00:06-07:00] [user] [discord] Hello there"
        )
        assert result == {
            "role": "user",
            "content": "Hello there",
            "timestamp": "2026-03-22T14:00:06-07:00",
            "channel": "discord",
        }

    def test_parses_assistant(self):
        logger = ConversationLogger()
        result = logger._parse_log_line(
            "[2026-03-22T14:00:06-07:00] [assistant] [sms] Hi back"
        )
        assert result["role"] == "assistant"
        assert result["channel"] == "sms"

    def test_unescapes_newlines(self):
        logger = ConversationLogger()
        result = logger._parse_log_line(
            "[2026-03-22T14:00:06-07:00] [user] [discord] Line 1\\nLine 2"
        )
        assert result["content"] == "Line 1\nLine 2"

    def test_returns_none_for_invalid(self):
        logger = ConversationLogger()
        assert logger._parse_log_line("not a valid log line") is None
        assert logger._parse_log_line("") is None
