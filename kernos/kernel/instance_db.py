"""Instance database — shared state across all instances.

data/instance.db — created at startup if it doesn't exist.
Starts nearly empty in V1 (just the owner as a member).
The architectural slot exists for multi-instance without a second migration.

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
    instance_id   TEXT DEFAULT '',        -- maps to per-instance DB
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

CREATE TABLE IF NOT EXISTS invite_codes (
    code         TEXT PRIMARY KEY,
    created_by   TEXT NOT NULL,
    for_member   TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    role         TEXT DEFAULT 'member',
    status       TEXT DEFAULT 'active',
    used_by      TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT DEFAULT '',
    used_at      TEXT DEFAULT ''
);
"""


class InstanceDB:
    """Manages the instance-level database shared across all instances."""

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
                           instance_id: str, platform: str, channel_id: str) -> None:
        """Ensure the owner exists as a member. Called at startup."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            "INSERT INTO members (member_id, display_name, role, instance_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'owner', ?, 'active', ?, ?) "
            "ON CONFLICT(member_id) DO UPDATE SET display_name=?, updated_at=?",
            (member_id, display_name, instance_id, now, now, display_name, now),
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
        members = []
        for r in rows:
            m = dict(r)
            # Attach connected channels
            async with self._conn.execute(
                "SELECT platform, channel_id FROM member_channels WHERE member_id=?",
                (m["member_id"],),
            ) as ch_cur:
                channels = await ch_cur.fetchall()
            m["channels"] = [dict(c) for c in channels]
            members.append(m)
        return members

    # --- Invite Codes ---

    async def create_invite_code(
        self, created_by: str, for_member: str = "",
        display_name: str = "", role: str = "member",
        expires_hours: int = 72,
    ) -> str:
        """Generate and store an invite code. Returns the code."""
        if not self._conn:
            return ""
        import random
        import string
        from kernos.utils import utc_now
        from datetime import datetime, timezone, timedelta

        chars = string.ascii_uppercase + string.digits
        code = "KERN-" + "".join(random.choices(chars, k=4))
        now = utc_now()
        expires = ""
        if expires_hours > 0:
            expires = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()

        await self._conn.execute(
            "INSERT INTO invite_codes "
            "(code, created_by, for_member, display_name, role, status, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, created_by, for_member, display_name, role, "active", now, expires),
        )
        await self._conn.commit()
        logger.info("INVITE_CODE_CREATED: code=%s by=%s for=%s name=%s expires=%s",
            code, created_by, for_member or "new_user", display_name, expires[:10])
        return code

    async def claim_invite_code(
        self, code: str, platform: str, channel_id: str,
    ) -> dict | None:
        """Attempt to claim an invite code. Returns result dict or None if invalid."""
        if not self._conn:
            return None
        from kernos.utils import utc_now
        from datetime import datetime, timezone
        import uuid

        async with self._conn.execute(
            "SELECT * FROM invite_codes WHERE code=? AND status='active'",
            (code,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        d = dict(row)

        # Check expiry
        if d.get("expires_at"):
            try:
                expires = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    await self._conn.execute(
                        "UPDATE invite_codes SET status='expired' WHERE code=?", (code,))
                    await self._conn.commit()
                    return None
            except (ValueError, TypeError):
                pass

        now = utc_now()
        used_by = f"{platform}:{channel_id}"

        if d.get("for_member"):
            # CHANNEL ADD — link channel to existing member
            member_id = d["for_member"]
            await self.register_channel(member_id, platform, channel_id)
            await self._conn.execute(
                "UPDATE invite_codes SET status='used', used_by=?, used_at=? WHERE code=?",
                (used_by, now, code),
            )
            await self._conn.commit()
            logger.info("INVITE_CLAIMED: code=%s action=channel_add member=%s platform=%s",
                code, member_id, platform)
            return {
                "action": "channel_add",
                "member_id": member_id,
                "static_response": "Connected! This channel is now linked to your Kernos account.",
            }
        else:
            # NEW USER — create member + register channel
            member_id = f"mem_{uuid.uuid4().hex[:8]}"
            display_name = d.get("display_name", "")
            role = d.get("role", "member")
            await self.create_member(member_id, display_name, role, "")
            await self.register_channel(member_id, platform, channel_id)
            await self._conn.execute(
                "UPDATE invite_codes SET status='used', used_by=?, used_at=? WHERE code=?",
                (used_by, now, code),
            )
            await self._conn.commit()
            logger.info("MEMBER_CREATED: member_id=%s name=%s code=%s platform=%s",
                member_id, display_name, code, platform)
            return {
                "action": "new_member",
                "member_id": member_id,
                "display_name": display_name,
                "static_response": (
                    f"Welcome to Kernos{', ' + display_name if display_name else ''}! "
                    f"You're all set. Say anything to get started."
                ),
            }

    async def create_member(
        self, member_id: str, display_name: str, role: str, instance_id: str,
    ) -> None:
        """Create a new member record."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            "INSERT INTO members (member_id, display_name, role, instance_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(member_id) DO NOTHING",
            (member_id, display_name, role, instance_id, "active", now, now),
        )
        await self._conn.commit()

    async def register_channel(
        self, member_id: str, platform: str, channel_id: str,
    ) -> None:
        """Register a new channel for an existing member."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        await self._conn.execute(
            "INSERT INTO member_channels (member_id, platform, channel_id, is_primary, created_at) "
            "VALUES (?,?,?,0,?) ON CONFLICT(platform, channel_id) DO NOTHING",
            (member_id, platform, channel_id, utc_now()),
        )
        await self._conn.commit()

    async def deactivate_member(self, member_id: str) -> bool:
        """Set member status to inactive. Returns True if found."""
        if not self._conn:
            return False
        from kernos.utils import utc_now
        result = await self._conn.execute(
            "UPDATE members SET status='inactive', updated_at=? WHERE member_id=?",
            (utc_now(), member_id),
        )
        await self._conn.commit()
        return result.rowcount > 0
