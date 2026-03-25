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


# ---------------------------------------------------------------------------
# P3: Compaction support — get_current_log_info, read_current_log_text, roll_log
# ---------------------------------------------------------------------------


class TestGetCurrentLogInfo:
    async def test_returns_info_with_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Hello")

        info = await logger.get_current_log_info("t1", "s1")
        assert info["log_number"] == 1
        assert info["tokens_est"] > 0
        assert info["exists"] is True

    async def test_returns_info_without_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        info = await logger.get_current_log_info("t1", "s1")
        assert info["log_number"] == 1
        assert info["tokens_est"] == 0
        assert info["exists"] is False


class TestReadCurrentLogText:
    async def test_reads_full_text(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Hello")
        await logger.append("t1", "s1", "assistant", "discord", "Hi!")

        text, num = await logger.read_current_log_text("t1", "s1")
        assert num == 1
        assert "[user]" in text
        assert "[assistant]" in text
        assert "Hello" in text
        assert "Hi!" in text

    async def test_raises_when_no_file(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            await logger.read_current_log_text("t1", "s1")


class TestRollLog:
    async def test_advances_log_number(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Before roll")

        old_num, new_num = await logger.roll_log("t1", "s1")
        assert old_num == 1
        assert new_num == 2

        meta = logger._load_meta("t1", "s1")
        assert meta["current_log"] == 2
        assert meta["current_log_tokens_est"] == 0

    async def test_new_appends_go_to_new_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "In log 1")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "In log 2")

        # log_001 should have first message
        log1 = tmp_path / "tenants" / "t1" / "spaces" / "s1" / "logs" / "log_001.txt"
        assert "In log 1" in log1.read_text()

        # log_002 should have second message
        log2 = tmp_path / "tenants" / "t1" / "spaces" / "s1" / "logs" / "log_002.txt"
        assert "In log 2" in log2.read_text()
        assert "In log 1" not in log2.read_text()

    async def test_read_recent_reads_from_new_log(self, tmp_path):
        """After rolling, read_recent reads from the new current log."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Old message")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "New message")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "New message"


class TestSeedFromPrevious:
    async def test_seeds_tail_lines(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        for i in range(20):
            await logger.append("t1", "s1", "user", "discord", f"Msg {i}")
        await logger.roll_log("t1", "s1")

        seeded = await logger.seed_from_previous("t1", "s1", 1, tail_lines=5)
        assert seeded == 5

        # New current log should have the last 5 messages
        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 5
        assert entries[0]["content"] == "Msg 15"
        assert entries[4]["content"] == "Msg 19"

    async def test_seeds_all_when_fewer_than_tail(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Only one")
        await logger.roll_log("t1", "s1")

        seeded = await logger.seed_from_previous("t1", "s1", 1, tail_lines=10)
        assert seeded == 1

    async def test_returns_zero_for_missing_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        seeded = await logger.seed_from_previous("t1", "s1", 99, tail_lines=10)
        assert seeded == 0

    async def test_updates_token_estimate(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "A" * 100)
        await logger.roll_log("t1", "s1")

        meta_before = logger._load_meta("t1", "s1")
        assert meta_before["current_log_tokens_est"] == 0

        await logger.seed_from_previous("t1", "s1", 1)
        meta_after = logger._load_meta("t1", "s1")
        assert meta_after["current_log_tokens_est"] > 0

    async def test_seed_doesnt_trigger_immediate_recompaction(self, tmp_path):
        """Seed tokens should be well below the 8000 threshold."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        for i in range(10):
            await logger.append("t1", "s1", "user", "discord", f"Message {i} with content")
        await logger.roll_log("t1", "s1")
        await logger.seed_from_previous("t1", "s1", 1, tail_lines=10)

        meta = logger._load_meta("t1", "s1")
        assert meta["current_log_tokens_est"] < 8000


# ---------------------------------------------------------------------------
# P4: read_log_text + remember_details
# ---------------------------------------------------------------------------


class TestReadLogText:
    async def test_reads_archived_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "In log 1")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "In log 2")

        # Read archived log 1
        text = await logger.read_log_text("t1", "s1", 1)
        assert text is not None
        assert "In log 1" in text

        # Read current log 2
        text2 = await logger.read_log_text("t1", "s1", 2)
        assert text2 is not None
        assert "In log 2" in text2

    async def test_returns_none_for_missing(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        assert await logger.read_log_text("t1", "s1", 99) is None


class TestParseLogRef:
    def test_standard_format(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("log_003") == 3

    def test_bare_number(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("3") == 3

    def test_no_underscore(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("log003") == 3

    def test_short_format(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("log_3") == 3

    def test_invalid(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("abc") is None
        assert ReasoningService._parse_log_ref("") is None


class TestExtractRelevantSection:
    def test_finds_matching_lines_with_context(self):
        from kernos.kernel.reasoning import ReasoningService
        log = "\n".join([
            "[ts] [user] [discord] Line 1",
            "[ts] [user] [discord] Line 2",
            "[ts] [user] [discord] Henderson deal discussion",
            "[ts] [user] [discord] Line 4",
            "[ts] [user] [discord] Line 5",
        ])
        result = ReasoningService._extract_relevant_section(log, "Henderson", context_lines=1)
        assert "Henderson" in result
        assert "Line 2" in result  # context before
        assert "Line 4" in result  # context after

    def test_returns_empty_on_no_match(self):
        from kernos.kernel.reasoning import ReasoningService
        result = ReasoningService._extract_relevant_section("no match here", "Henderson")
        assert result == ""


class TestRememberDetailsHandler:
    async def test_no_source_ref_returns_guidance(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        result = await svc._handle_remember_details("t1", "s1", {"source_ref": ""})
        assert "remember()" in result

    async def test_invalid_ref_returns_error(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        result = await svc._handle_remember_details("t1", "s1", {"source_ref": "xyz"})
        assert "Could not parse" in result

    async def test_retrieves_from_archived_log(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock, AsyncMock
        from kernos.kernel.conversation_log import ConversationLogger

        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        await conv_logger.append("t1", "s1", "user", "discord", "Henderson discussed")
        await conv_logger.roll_log("t1", "s1")

        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler_mock = MagicMock()
        handler_mock.conv_logger = conv_logger
        svc._handler = handler_mock

        result = await svc._handle_remember_details("t1", "s1", {
            "source_ref": "log_001",
            "query": "Henderson",
        })
        assert "Henderson" in result
        assert "log_001" in result

    async def test_full_log_without_query(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        from kernos.kernel.conversation_log import ConversationLogger

        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        await conv_logger.append("t1", "s1", "user", "discord", "Short log")

        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler_mock = MagicMock()
        handler_mock.conv_logger = conv_logger
        svc._handler = handler_mock

        result = await svc._handle_remember_details("t1", "s1", {
            "source_ref": "1",
        })
        assert "Short log" in result
        assert "full log" in result
