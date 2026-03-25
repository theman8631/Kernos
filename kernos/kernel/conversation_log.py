"""Per-space conversation log files.

Phase 1: write — append-only plain-text logs alongside existing conversation store.
Phase 2: read — handler reads context from space logs instead of channel JSON.
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ConversationLogger:
    """Per-space conversation log files.

    P1: Writes user/assistant turns to numbered plain-text log files.
    P2: Reads recent entries with token-budgeted windowing for context assembly.
    """

    def __init__(self, data_dir: str = "./data") -> None:
        self._data_dir = Path(data_dir)

    def _logs_dir(self, tenant_id: str, space_id: str) -> Path:
        return self._data_dir / "tenants" / tenant_id / "spaces" / space_id / "logs"

    def _meta_path(self, tenant_id: str, space_id: str) -> Path:
        return self._logs_dir(tenant_id, space_id) / "meta.json"

    def _load_meta(self, tenant_id: str, space_id: str) -> dict:
        path = self._meta_path(tenant_id, space_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "current_log": 1,
            "current_log_tokens_est": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_meta(self, tenant_id: str, space_id: str, meta: dict) -> None:
        path = self._meta_path(tenant_id, space_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def _current_log_path(self, tenant_id: str, space_id: str) -> Path:
        meta = self._load_meta(tenant_id, space_id)
        num = meta["current_log"]
        return self._logs_dir(tenant_id, space_id) / f"log_{num:03d}.txt"

    def _escape_newlines(self, text: str) -> str:
        """Replace actual newlines with literal \\n for single-line log entries."""
        return text.replace("\n", "\\n")

    async def append(
        self,
        tenant_id: str,
        space_id: str,
        speaker: str,       # "user" or "assistant"
        channel: str,        # "discord", "sms", "scheduled", "whisper", "system"
        content: str,
        timestamp: str = "",  # ISO 8601, defaults to now
    ) -> None:
        """Append a single line to the current log file for this space."""
        if not space_id:
            return

        try:
            logs_dir = self._logs_dir(tenant_id, space_id)
            logs_dir.mkdir(parents=True, exist_ok=True)

            ts = timestamp or datetime.now(timezone.utc).isoformat()
            escaped = self._escape_newlines(content)
            line = f"[{ts}] [{speaker}] [{channel}] {escaped}\n"

            log_path = self._current_log_path(tenant_id, space_id)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)

            # Update estimated token count (rough: 1 token ≈ 4 chars)
            meta = self._load_meta(tenant_id, space_id)
            meta["current_log_tokens_est"] += len(line) // 4
            self._save_meta(tenant_id, space_id, meta)

            logger.info(
                "CONV_LOG: space=%s log=%03d speaker=%s channel=%s len=%d",
                space_id, meta["current_log"], speaker, channel, len(content),
            )
        except Exception as exc:
            # Never break the user's message flow for logging failures
            logger.warning("CONV_LOG: failed to write: %s", exc)

    # --- P2: Read ---

    _LOG_LINE_RE = re.compile(r'\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(.*)')

    async def read_recent(
        self,
        tenant_id: str,
        space_id: str,
        token_budget: int = 4000,
        max_messages: int = 50,
    ) -> list[dict]:
        """Read recent conversation entries from the current space log.

        Walks backward from the tail of the current log file until either
        token_budget or max_messages is exhausted. Returns entries in
        chronological order (oldest first).

        Returns list of dicts: {role, content, timestamp, channel}.
        The HANDLER owns conversion into the exact reasoning message format.
        """
        if not space_id:
            return []

        log_path = self._current_log_path(tenant_id, space_id)
        if not log_path.exists():
            return []

        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        entries: list[dict] = []
        tokens_used = 0

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue

            parsed = self._parse_log_line(line)
            if parsed is None:
                continue

            # Token estimate from the FULL rendered line (speaker + channel + content)
            entry_tokens = len(line) // 4
            if tokens_used + entry_tokens > token_budget and entries:
                break  # Budget exhausted (always include at least one)

            tokens_used += entry_tokens
            entries.append(parsed)

            if len(entries) >= max_messages:
                break

        entries.reverse()
        return entries

    def _parse_log_line(self, line: str) -> dict | None:
        """Parse a strict-format log line into a structured dict.

        Input:  [2026-03-22T14:00:06-07:00] [user] [discord] Hello there
        Output: {"role": "user", "content": "Hello there",
                 "timestamp": "2026-03-22T14:00:06-07:00", "channel": "discord"}

        Unescapes \\n back to real newlines (P1 escapes on write).
        """
        match = self._LOG_LINE_RE.match(line)
        if not match:
            return None

        timestamp, speaker, channel, content = match.groups()

        # Unescape multiline content
        content = content.replace("\\n", "\n")

        # Map speaker to role
        role = "user" if speaker == "user" else "assistant"

        return {
            "role": role,
            "content": content,
            "timestamp": timestamp,
            "channel": channel,
        }

    # --- P3: Compaction support ---

    async def get_current_log_info(
        self, tenant_id: str, space_id: str,
    ) -> dict:
        """Get metadata about the current log.

        Returns: {"log_number": N, "tokens_est": N, "path": Path, "exists": bool}
        """
        meta = self._load_meta(tenant_id, space_id)
        log_path = self._logs_dir(tenant_id, space_id) / f"log_{meta['current_log']:03d}.txt"
        return {
            "log_number": meta["current_log"],
            "tokens_est": meta.get("current_log_tokens_est", 0),
            "path": log_path,
            "exists": log_path.exists(),
        }

    async def read_current_log_text(
        self, tenant_id: str, space_id: str,
    ) -> tuple[str, int]:
        """Read the full text of the current log file.

        Returns: (log_text, log_number)
        Raises FileNotFoundError if no log exists.
        """
        meta = self._load_meta(tenant_id, space_id)
        log_num = meta["current_log"]
        log_path = self._logs_dir(tenant_id, space_id) / f"log_{log_num:03d}.txt"
        if not log_path.exists():
            raise FileNotFoundError(f"No log file at {log_path}")
        text = log_path.read_text(encoding="utf-8")
        return text, log_num

    async def roll_log(
        self, tenant_id: str, space_id: str,
    ) -> tuple[int, int]:
        """Close current log for appends and start a new one.

        Returns: (old_log_number, new_log_number)
        """
        meta = self._load_meta(tenant_id, space_id)
        old_num = meta["current_log"]
        new_num = old_num + 1

        meta["current_log"] = new_num
        meta["current_log_tokens_est"] = 0
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        self._save_meta(tenant_id, space_id, meta)

        logger.info(
            "LOG_ROLL: space=%s closed=log_%03d starting=log_%03d",
            space_id, old_num, new_num,
        )
        return old_num, new_num

    async def read_log_text(
        self, tenant_id: str, space_id: str, log_number: int,
    ) -> str | None:
        """Read the full text of an archived or current log file.

        Returns the log text, or None if the file doesn't exist.
        Public API — used by remember_details handler.
        """
        log_path = self._logs_dir(tenant_id, space_id) / f"log_{log_number:03d}.txt"
        if not log_path.exists():
            return None
        return log_path.read_text(encoding="utf-8")
