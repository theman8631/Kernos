"""Instance database — shared state across all tenants.

data/instance.db — created at startup if it doesn't exist.
Starts nearly empty in V1 (just the owner as a member).
The architectural slot exists for multi-tenant without a second migration.

Tables:
  members         — who belongs to this Kernos instance
  member_channels  — phone number / discord ID → member mapping
  message_relay   — cross-member message queue (V2)
  shared_spaces   — instance-level shared space registry (V2)
"""
import json
import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_INSTANCE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS members (
    member_id   TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    role        TEXT DEFAULT 'member',  -- 'owner' | 'member' | 'guest'
    tenant_id   TEXT DEFAULT '',        -- maps to per-tenant DB
    status      TEXT DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT NOT NULL,
    platform    TEXT NOT NULL,           -- 'discord' | 'sms' | 'whatsapp'
    channel_id  TEXT NOT NULL,           -- phone number, discord user ID, etc.
    is_primary  INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (member_id) REFERENCES members(member_id),
    UNIQUE(platform, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_channels_lookup ON member_channels(platform, channel_id);

CREATE TABLE IF NOT EXISTS message_relay (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_member TEXT NOT NULL,
    to_member   TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',  -- 'pending' | 'delivered' | 'expired'
    created_at  TEXT NOT NULL,
    delivered_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_relay_pending ON message_relay(to_member, status)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS shared_spaces (
    space_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class InstanceDB:
    """Manages the instance-level database shared across all tenants."""

    def __init__(self, data_dir: str) -> None:
        self._db_path = Path(data_dir) / "instance.db"
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initialize the instance database."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        for stmt in _INSTANCE_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        await self._conn.commit()
        logger.info("Instance DB ready: %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def ensure_owner(self, member_id: str, display_name: str,
                           tenant_id: str, platform: str, channel_id: str) -> None:
        """Ensure the owner exists as a member. Called at startup."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            "INSERT INTO members (member_id, display_name, role, tenant_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'owner', ?, 'active', ?, ?) "
            "ON CONFLICT(member_id) DO UPDATE SET display_name=?, updated_at=?",
            (member_id, display_name, tenant_id, now, now, display_name, now),
        )
        await self._conn.execute(
            "INSERT INTO member_channels (member_id, platform, channel_id, is_primary, created_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(platform, channel_id) DO NOTHING",
            (member_id, platform, channel_id, now),
        )
        await self._conn.commit()

    async def get_member_by_channel(self, platform: str, channel_id: str) -> dict | None:
        """Look up a member by platform + channel ID."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT m.* FROM members m "
            "JOIN member_channels mc ON m.member_id = mc.member_id "
            "WHERE mc.platform=? AND mc.channel_id=?",
            (platform, channel_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_members(self, status: str = "active") -> list[dict]:
        """List all members with the given status."""
        if not self._conn:
            return []
        async with self._conn.execute(
            "SELECT * FROM members WHERE status=?", (status,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
