"""JSON-on-disk implementation of all three persistence stores.

All methods are async def. File I/O is synchronous underneath.
filelock prevents corruption from concurrent writes.

The handler imports from kernos.persistence (interfaces), not this module directly.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from kernos.persistence.base import AuditStore, ConversationStore, TenantStore

logger = logging.getLogger(__name__)

# Archive subdirectories created from day one per Blueprint mandate.
_ARCHIVE_SUBDIRS = [
    "conversations",
    "email",
    "files",
    "calendar",
    "contacts",
    "memory",
    "agents",
]


def _safe_name(s: str) -> str:
    """Convert a string to a safe filesystem name.

    Replaces characters that may cause issues in file paths.
    ':' is valid on Linux but we replace it for cross-platform safety and readability.
    """
    return s.replace(":", "_").replace("/", "_").replace("\\", "_")


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_tenant_dirs(tenant_root: Path) -> None:
    """Create the full tenant directory structure including all archive subdirs."""
    (tenant_root / "conversations").mkdir(parents=True, exist_ok=True)
    (tenant_root / "audit").mkdir(parents=True, exist_ok=True)
    archive = tenant_root / "archive"
    for subdir in _ARCHIVE_SUBDIRS:
        (archive / subdir).mkdir(parents=True, exist_ok=True)


class JsonConversationStore(ConversationStore):
    """JSON file-backed conversation history store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _conversation_path(self, tenant_id: str, conversation_id: str) -> Path:
        return (
            self._data_dir
            / _safe_name(tenant_id)
            / "conversations"
            / f"{_safe_name(conversation_id)}.json"
        )

    async def append(self, tenant_id: str, conversation_id: str, entry: dict) -> None:
        path = self._conversation_path(tenant_id, conversation_id)
        # Ensure directories exist (safety net — TenantStore.get_or_create runs first normally)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            else:
                entries = []
            entries.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)

    async def get_recent(
        self, tenant_id: str, conversation_id: str, limit: int = 20
    ) -> list[dict]:
        path = self._conversation_path(tenant_id, conversation_id)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        recent = entries[-limit:] if len(entries) > limit else entries
        # Return only role and content — full metadata stays on disk
        return [{"role": e["role"], "content": e["content"]} for e in recent]

    async def archive(self, tenant_id: str, conversation_id: str) -> None:
        path = self._conversation_path(tenant_id, conversation_id)
        if not path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_dir = (
            self._data_dir
            / _safe_name(tenant_id)
            / "archive"
            / "conversations"
            / timestamp
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{_safe_name(conversation_id)}.json"

        with open(path, "r", encoding="utf-8") as f:
            original = json.load(f)

        archived = {
            "archived_at": _now_iso(),
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "entries": original,
        }
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(archived, f, ensure_ascii=False, indent=2)

        # Relocate — not delete. Original removed, archive copy preserved.
        path.unlink()
        logger.info(
            "Archived conversation %s for tenant %s to %s",
            conversation_id,
            tenant_id,
            dest,
        )


class JsonTenantStore(TenantStore):
    """JSON file-backed tenant record store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _tenant_path(self, tenant_id: str) -> Path:
        return self._data_dir / _safe_name(tenant_id) / "tenant.json"

    async def get_or_create(self, tenant_id: str) -> dict:
        path = self._tenant_path(tenant_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        # Auto-provision: create the full directory structure and tenant record.
        tenant_root = self._data_dir / _safe_name(tenant_id)
        _ensure_tenant_dirs(tenant_root)

        record: dict = {
            "tenant_id": tenant_id,
            "status": "active",
            "created_at": _now_iso(),
            "capabilities": {},
        }
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            # Check again inside the lock (race condition guard)
            if not path.exists():
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    record = json.load(f)

        logger.info("Auto-provisioned new tenant: %s", tenant_id)
        return record

    async def save(self, tenant_id: str, record: dict) -> None:
        path = self._tenant_path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)


class JsonAuditStore(AuditStore):
    """JSON file-backed audit log store, partitioned by date."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _audit_path(self, tenant_id: str) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._data_dir / _safe_name(tenant_id) / "audit" / f"{date_str}.json"

    async def log(self, tenant_id: str, entry: dict) -> None:
        path = self._audit_path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            else:
                entries = []
            entries.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
