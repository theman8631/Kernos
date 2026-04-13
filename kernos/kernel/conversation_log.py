"""Per-space conversation log files.

Phase 1: write — append-only plain-text logs alongside existing conversation store.
Phase 2: read — handler reads context from space logs instead of channel JSON.

Concurrency: per-space asyncio Lock serializes writes (append, roll, seed).
Reads are eventually consistent (no lock). Single-process only.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Pattern matching the start of a log entry: [timestamp] [speaker] [channel]
_ENTRY_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+'
)


def _parse_entries(text: str) -> list[dict]:
    """Parse log text into structured entries, handling multiline content.

    Each entry starts with [timestamp] [speaker] [channel] on a new line.
    Everything until the next entry header is the content (including real newlines).
    """
    lines = text.split("\n")
    entries: list[dict] = []
    current: dict | None = None

    for line in lines:
        match = _ENTRY_RE.match(line)
        if match:
            if current:
                entries.append(current)
            timestamp, speaker, channel = match.groups()
            content_start = match.end()
            role = "user" if speaker == "user" else "assistant"
            current = {
                "role": role,
                "content": line[content_start:],
                "timestamp": timestamp,
                "channel": channel,
            }
        elif current:
            # Continuation line — append to current entry's content
            current["content"] += "\n" + line

    if current:
        entries.append(current)

    # Strip trailing whitespace from content
    for entry in entries:
        entry["content"] = entry["content"].rstrip()

    return entries


class ConversationLogger:
    """Per-space conversation log files.

    P1: Writes user/assistant turns to numbered plain-text log files.
    P2: Reads recent entries with token-budgeted windowing for context assembly.
    """

    def __init__(self, data_dir: str = "./data") -> None:
        self._data_dir = Path(data_dir)
        self._meta_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, instance_id: str, space_id: str) -> asyncio.Lock:
        """Per-space asyncio lock for serializing meta read-modify-write."""
        key = f"{instance_id}:{space_id}"
        if key not in self._meta_locks:
            self._meta_locks[key] = asyncio.Lock()
        return self._meta_locks[key]

    def _logs_dir(self, instance_id: str, space_id: str) -> Path:
        return self._data_dir / "tenants" / instance_id / "spaces" / space_id / "logs"

    def _meta_path(self, instance_id: str, space_id: str) -> Path:
        return self._logs_dir(instance_id, space_id) / "meta.json"

    def _load_meta(self, instance_id: str, space_id: str) -> dict:
        path = self._meta_path(instance_id, space_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "current_log": 1,
            "current_log_tokens_est": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_meta(self, instance_id: str, space_id: str, meta: dict) -> None:
        """Atomic write: tempfile + os.replace (POSIX atomic)."""
        path = self._meta_path(instance_id, space_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _current_log_path(self, instance_id: str, space_id: str) -> Path:
        meta = self._load_meta(instance_id, space_id)
        num = meta["current_log"]
        return self._logs_dir(instance_id, space_id) / f"log_{num:03d}.txt"

    async def append(
        self,
        instance_id: str,
        space_id: str,
        speaker: str,       # "user" or "assistant"
        channel: str,        # "discord", "sms", "scheduled", "whisper", "system"
        content: str,
        timestamp: str = "",  # ISO 8601, defaults to now
    ) -> None:
        """Append an entry to the current log file for this space.

        Content is written with real newlines — no escaping.
        Each entry starts with a [timestamp] [speaker] [channel] header line.
        """
        if not space_id:
            return

        try:
            async with self._get_lock(instance_id, space_id):
                logs_dir = self._logs_dir(instance_id, space_id)
                logs_dir.mkdir(parents=True, exist_ok=True)

                ts = timestamp or datetime.now(timezone.utc).isoformat()
                line = f"[{ts}] [{speaker}] [{channel}] {content}\n"

                log_path = self._current_log_path(instance_id, space_id)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)

                # Update estimated token count (rough: 1 token ≈ 4 chars)
                meta = self._load_meta(instance_id, space_id)
                meta["current_log_tokens_est"] += len(line) // 4
                self._save_meta(instance_id, space_id, meta)

                logger.info(
                    "CONV_LOG: space=%s log=%03d speaker=%s channel=%s len=%d",
                    space_id, meta["current_log"], speaker, channel, len(content),
                )
        except Exception as exc:
            # Never break the user's message flow for logging failures
            logger.warning("CONV_LOG: failed to write: %s", exc)

    # --- P2: Read ---

    async def read_recent(
        self,
        instance_id: str,
        space_id: str,
        token_budget: int = 4000,
        max_messages: int = 50,
    ) -> list[dict]:
        """Read recent conversation entries from the current space log.

        Walks backward from the tail of parsed entries until either
        token_budget or max_messages is exhausted. Returns entries in
        chronological order (oldest first).

        Returns list of dicts: {role, content, timestamp, channel}.
        """
        if not space_id:
            return []

        log_path = self._current_log_path(instance_id, space_id)
        if not log_path.exists():
            return []

        text = log_path.read_text(encoding="utf-8")
        all_entries = _parse_entries(text)

        result: list[dict] = []
        tokens_used = 0

        for entry in reversed(all_entries):
            entry_tokens = len(entry["content"]) // 4 + 10  # content + header overhead
            if tokens_used + entry_tokens > token_budget and result:
                break
            tokens_used += entry_tokens
            result.append(entry)
            if len(result) >= max_messages:
                break

        result.reverse()
        return result

    # --- P3: Compaction support ---

    async def get_current_log_info(
        self, instance_id: str, space_id: str,
    ) -> dict:
        """Get metadata about the current log.

        Returns: {"log_number": N, "tokens_est": N, "path": Path, "exists": bool}
        """
        meta = self._load_meta(instance_id, space_id)
        log_path = self._logs_dir(instance_id, space_id) / f"log_{meta['current_log']:03d}.txt"
        return {
            "log_number": meta["current_log"],
            "tokens_est": meta.get("current_log_tokens_est", 0),
            "seeded_tokens_est": meta.get("seeded_tokens_est", 0),
            "path": log_path,
            "exists": log_path.exists(),
        }

    async def read_current_log_text(
        self, instance_id: str, space_id: str,
    ) -> tuple[str, int]:
        """Read the full text of the current log file.

        Returns: (log_text, log_number)
        Raises FileNotFoundError if no log exists.
        """
        meta = self._load_meta(instance_id, space_id)
        log_num = meta["current_log"]
        log_path = self._logs_dir(instance_id, space_id) / f"log_{log_num:03d}.txt"
        if not log_path.exists():
            raise FileNotFoundError(f"No log file at {log_path}")
        text = log_path.read_text(encoding="utf-8")
        return text, log_num

    async def roll_log(
        self, instance_id: str, space_id: str,
    ) -> tuple[int, int]:
        """Close current log for appends and start a new one.

        Returns: (old_log_number, new_log_number)
        """
        async with self._get_lock(instance_id, space_id):
            meta = self._load_meta(instance_id, space_id)
            old_num = meta["current_log"]
            new_num = old_num + 1

            meta["current_log"] = new_num
            meta["current_log_tokens_est"] = 0
            meta["seeded_tokens_est"] = 0
            meta["created_at"] = datetime.now(timezone.utc).isoformat()
            self._save_meta(instance_id, space_id, meta)

            logger.info(
                "LOG_ROLL: space=%s closed=log_%03d starting=log_%03d",
                space_id, old_num, new_num,
            )
            return old_num, new_num

    async def seed_from_previous(
        self, instance_id: str, space_id: str,
        previous_log_number: int, tail_entries: int = 10,
    ) -> int:
        """Copy last N entries from archived log into new current log.

        Preserves recent context across compaction boundaries so the agent
        doesn't lose track of the conversation.

        Returns number of entries seeded. Updates meta.json token estimate.
        """
        async with self._get_lock(instance_id, space_id):
            return await self._seed_from_previous_locked(
                instance_id, space_id, previous_log_number, tail_entries,
            )

    async def _seed_from_previous_locked(
        self, instance_id: str, space_id: str,
        previous_log_number: int, tail_entries: int,
    ) -> int:
        """Internal seed implementation — must be called under lock."""
        prev_path = self._logs_dir(instance_id, space_id) / f"log_{previous_log_number:03d}.txt"
        if not prev_path.exists():
            return 0

        text = prev_path.read_text(encoding="utf-8")
        all_entries = _parse_entries(text)
        if not all_entries:
            return 0

        seed_entries = all_entries[-tail_entries:]

        # Reconstruct log lines for seeded entries
        current_path = self._current_log_path(instance_id, space_id)
        current_path.parent.mkdir(parents=True, exist_ok=True)
        seed_text = ""
        for entry in seed_entries:
            speaker = "user" if entry["role"] == "user" else "assistant"
            line = f"[{entry['timestamp']}] [{speaker}] [{entry['channel']}] {entry['content']}\n"
            seed_text += line

        with open(current_path, "a", encoding="utf-8") as f:
            f.write(seed_text)

        # Update token estimate — track seeded tokens separately
        seed_tokens = len(seed_text) // 4
        meta = self._load_meta(instance_id, space_id)
        meta["current_log_tokens_est"] += seed_tokens
        meta["seeded_tokens_est"] = meta.get("seeded_tokens_est", 0) + seed_tokens
        self._save_meta(instance_id, space_id, meta)

        logger.info(
            "LOG_SEED: space=%s from=log_%03d entries=%d tokens_est=%d",
            space_id, previous_log_number, len(seed_entries), len(seed_text) // 4,
        )
        return len(seed_entries)

    async def read_log_text(
        self, instance_id: str, space_id: str, log_number: int,
    ) -> str | None:
        """Read the full text of an archived or current log file.

        Returns the log text, or None if the file doesn't exist.
        Public API — used by remember_details handler.
        """
        log_path = self._logs_dir(instance_id, space_id) / f"log_{log_number:03d}.txt"
        if not log_path.exists():
            return None
        return log_path.read_text(encoding="utf-8")
