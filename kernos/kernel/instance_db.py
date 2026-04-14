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

CREATE TABLE IF NOT EXISTS member_profiles (
    member_id               TEXT PRIMARY KEY,
    display_name            TEXT DEFAULT '',
    timezone                TEXT DEFAULT '',
    communication_style     TEXT DEFAULT '',
    interaction_count       INTEGER DEFAULT 0,
    hatched                 INTEGER DEFAULT 0,
    hatched_at              TEXT DEFAULT '',
    bootstrap_graduated     INTEGER DEFAULT 0,
    bootstrap_graduated_at  TEXT DEFAULT '',
    agent_name              TEXT DEFAULT '',
    emoji                   TEXT DEFAULT '',
    personality_notes       TEXT DEFAULT '',
    updated_at              TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (member_id) REFERENCES members(member_id)
);

CREATE TABLE IF NOT EXISTS platform_config (
    platform    TEXT PRIMARY KEY,
    config      TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code         TEXT PRIMARY KEY,
    created_by   TEXT NOT NULL,
    for_member   TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    platform     TEXT DEFAULT '',
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
        # Migrations: add columns that may be missing from older databases
        for _alt in [
            "ALTER TABLE invite_codes ADD COLUMN platform TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN agent_name TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN emoji TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN personality_notes TEXT DEFAULT ''",
        ]:
            try:
                await self._conn.execute(_alt)
            except Exception:
                pass  # Column already exists
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
        # Ensure owner has a member profile
        existing_profile = await self.get_member_profile(member_id)
        if not existing_profile:
            await self.upsert_member_profile(member_id, {"display_name": display_name})

    async def migrate_soul_to_member_profile(self, member_id: str, soul_fields: dict) -> bool:
        """One-time migration: copy per-user Soul fields into the owner's member profile.

        Called at startup when transitioning from single-user to multi-member.
        Returns True if migration occurred, False if already migrated or nothing to migrate.
        """
        if not self._conn:
            return False
        profile = await self.get_member_profile(member_id)
        if not profile:
            return False
        # Only migrate if the profile has empty fields and soul has data
        updates: dict = {}
        field_map = {
            "user_name": "display_name",
            "timezone": "timezone",
            "communication_style": "communication_style",
            "interaction_count": "interaction_count",
            "bootstrap_graduated": "bootstrap_graduated",
            "bootstrap_graduated_at": "bootstrap_graduated_at",
            # Soul identity fields (Soul Revision spec)
            "agent_name": "agent_name",
            "emoji": "emoji",
            "personality_notes": "personality_notes",
            "hatched": "hatched",
            "hatched_at": "hatched_at",
        }
        for soul_field, profile_field in field_map.items():
            soul_val = soul_fields.get(soul_field)
            profile_val = profile.get(profile_field)
            if soul_val and not profile_val:
                updates[profile_field] = soul_val
        if updates:
            await self.upsert_member_profile(member_id, updates)
            logger.info("SOUL_MIGRATION: migrated %s to member_profile %s", list(updates.keys()), member_id)
            return True
        return False

    # Fallback invite instructions when platform_config is unavailable
    _INVITE_INSTRUCTIONS_FALLBACK: dict[str, str] = {
        "discord": (
            "Send this code as a DM to the Kernos bot on Discord. "
            "The bot will recognize the code and link your account."
        ),
        "telegram": (
            "Open Telegram, find the Kernos bot, and send this code as a message. "
            "The bot will recognize the code and link your account."
        ),
        "sms": (
            "Text this code to the Kernos phone number. "
            "The system will recognize the code and link your account."
        ),
    }

    async def get_invite_instructions(self, platform: str) -> str:
        """Get invite instructions with actual bot handle/phone interpolated."""
        config = await self.get_platform_config(platform)
        if platform == "telegram":
            username = config.get("bot_username", "")
            if username:
                return (
                    f"Open Telegram, search for @{username}, and send this code as a message. "
                    f"The bot will recognize the code and link your account."
                )
        elif platform == "discord":
            bot_name = config.get("bot_name", "")
            if bot_name:
                return (
                    f"Send this code as a DM to {bot_name} on Discord. "
                    f"The bot will recognize the code and link your account."
                )
        elif platform == "sms":
            phone = config.get("phone_number", "")
            if phone:
                return (
                    f"Text this code to {phone}. "
                    f"The system will recognize the code and link your account."
                )
        return self._INVITE_INSTRUCTIONS_FALLBACK.get(
            platform, f"Send this code on {platform} to connect."
        )

    _SETUP_INSTRUCTIONS: dict[str, str] = {
        "discord": (
            "To set up Discord:\n"
            "1. Go to the Discord Developer Portal (discord.com/developers/applications)\n"
            "2. Create a New Application, then go to the Bot section and create a bot\n"
            "3. Enable the Message Content Intent under Privileged Gateway Intents\n"
            "4. Copy the bot token and set DISCORD_BOT_TOKEN in .env\n"
            "5. Set DISCORD_OWNER_ID to your Discord user ID\n"
            "6. Invite the bot to your server using the OAuth2 URL Generator with bot scope + Send Messages permission\n"
            "7. Restart Kernos — the bot will appear online and respond to DMs"
        ),
        "telegram": (
            "To set up Telegram:\n"
            "1. Open Telegram and message @BotFather\n"
            "2. Send /newbot and follow the prompts to create a bot\n"
            "3. Copy the bot token BotFather gives you\n"
            "4. Set TELEGRAM_BOT_TOKEN in .env\n"
            "5. Restart Kernos — the Telegram poller will start and respond to messages"
        ),
        "sms": (
            "To set up SMS:\n"
            "1. Create a Twilio account at twilio.com\n"
            "2. Get a phone number from the Twilio console\n"
            "3. Set these in .env:\n"
            "   TWILIO_ACCOUNT_SID=your_account_sid\n"
            "   TWILIO_AUTH_TOKEN=your_auth_token\n"
            "   TWILIO_PHONE_NUMBER=+1XXXXXXXXXX\n"
            "   OWNER_PHONE_NUMBER=+1XXXXXXXXXX (your personal number)\n"
            "4. Restart Kernos — the SMS poller will start"
        ),
    }

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

    # --- Instance Config (stored in platform_config with key "_instance") ---

    async def get_hatching_mode(self) -> str:
        """Get the instance's hatching mode. Returns 'unique' (default) or 'inherit'."""
        config = await self.get_platform_config("_instance")
        return config.get("hatching_mode", "unique")

    async def set_hatching_mode(self, mode: str) -> None:
        """Set hatching mode: 'unique' or 'inherit'."""
        config = await self.get_platform_config("_instance")
        config["hatching_mode"] = mode
        await self.set_platform_config("_instance", config)

    async def get_template_soul(self) -> dict | None:
        """Get the first hatched member's soul fields for inherit mode."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM member_profiles WHERE hatched=1 ORDER BY hatched_at ASC LIMIT 1",
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = dict(row)
            return {
                "agent_name": d.get("agent_name", ""),
                "emoji": d.get("emoji", ""),
                "personality_notes": d.get("personality_notes", ""),
            }
        return None

    # --- Invite Codes ---

    def get_setup_instructions(self, platform: str) -> str:
        """Get first-time setup instructions for a platform."""
        return self._SETUP_INSTRUCTIONS.get(platform, f"Setup instructions for {platform} are not yet available.")

    def get_supported_platforms(self) -> list[str]:
        """Return all platforms that have known instructions."""
        return list(self._INVITE_INSTRUCTIONS_FALLBACK.keys())

    async def get_platform_config(self, platform: str) -> dict:
        """Get persisted config for a platform (bot username, phone number, etc.)."""
        if not self._conn:
            return {}
        async with self._conn.execute(
            "SELECT config FROM platform_config WHERE platform=?", (platform,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    async def set_platform_config(self, platform: str, config: dict) -> None:
        """Persist config for a platform (bot username, phone number, etc.)."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO platform_config (platform, config) VALUES (?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET config=?",
            (platform, json.dumps(config), json.dumps(config)),
        )
        await self._conn.commit()
        logger.info("Platform config stored: %s → %s", platform, list(config.keys()))

    # --- Member Profiles ---

    async def get_member_profile(self, member_id: str) -> dict | None:
        """Get a member's profile. Returns None if no profile exists."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM member_profiles WHERE member_id=?", (member_id,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = dict(row)
            # Normalize booleans from SQLite integers
            d["hatched"] = bool(d.get("hatched", 0))
            d["bootstrap_graduated"] = bool(d.get("bootstrap_graduated", 0))
            return d
        return None

    async def upsert_member_profile(self, member_id: str, updates: dict) -> None:
        """Create or update a member's profile fields."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        existing = await self.get_member_profile(member_id)
        if existing is None:
            # Create new profile
            fields = {
                "member_id": member_id,
                "display_name": updates.get("display_name", ""),
                "timezone": updates.get("timezone", ""),
                "communication_style": updates.get("communication_style", ""),
                "interaction_count": updates.get("interaction_count", 0),
                "hatched": int(updates.get("hatched", False)),
                "hatched_at": updates.get("hatched_at", ""),
                "bootstrap_graduated": int(updates.get("bootstrap_graduated", False)),
                "bootstrap_graduated_at": updates.get("bootstrap_graduated_at", ""),
                "updated_at": utc_now(),
            }
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            await self._conn.execute(
                f"INSERT INTO member_profiles ({cols}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        else:
            # Update existing fields
            set_parts: list[str] = []
            values: list = []
            for k, v in updates.items():
                if k == "member_id":
                    continue
                if k in ("hatched", "bootstrap_graduated"):
                    v = int(v)
                set_parts.append(f"{k}=?")
                values.append(v)
            set_parts.append("updated_at=?")
            values.append(utc_now())
            values.append(member_id)
            await self._conn.execute(
                f"UPDATE member_profiles SET {', '.join(set_parts)} WHERE member_id=?",
                values,
            )
        await self._conn.commit()

    async def increment_interaction_count(self, member_id: str) -> int:
        """Increment and return the new interaction count for a member."""
        if not self._conn:
            return 0
        from kernos.utils import utc_now
        await self._conn.execute(
            "UPDATE member_profiles SET interaction_count = interaction_count + 1, updated_at=? "
            "WHERE member_id=?",
            (utc_now(), member_id),
        )
        await self._conn.commit()
        profile = await self.get_member_profile(member_id)
        return profile["interaction_count"] if profile else 0

    async def get_member(self, member_id: str) -> dict | None:
        """Get a member record from the members table."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM members WHERE member_id=?", (member_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # --- Invite Codes ---

    async def create_invite_code(
        self, created_by: str, platform: str,
        for_member: str = "", display_name: str = "",
        role: str = "member", expires_hours: int = 72,
    ) -> dict:
        """Generate and store a platform-locked invite code.

        Returns dict with code, platform, instructions, expires_hours.
        """
        if not self._conn:
            return {"error": "Database not available"}
        if not platform:
            return {"error": "Platform is required — specify discord, telegram, or sms"}
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
            "(code, created_by, for_member, display_name, platform, role, status, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, created_by, for_member, display_name, platform, role, "active", now, expires),
        )
        await self._conn.commit()
        logger.info("INVITE_CODE_CREATED: code=%s by=%s for=%s name=%s platform=%s expires=%s",
            code, created_by, for_member or "new_user", display_name, platform, expires[:10])

        instructions = await self.get_invite_instructions(platform)
        return {
            "code": code,
            "platform": platform,
            "instructions": instructions,
            "expires_hours": expires_hours,
        }

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

        # Platform enforcement — code locked to specific platform
        code_platform = d.get("platform", "")
        if code_platform and code_platform != platform:
            logger.info("INVITE_REJECTED: code=%s expected_platform=%s actual_platform=%s",
                code, code_platform, platform)
            return {
                "action": "rejected",
                "static_response": f"This invite code is for {code_platform}, not {platform}.",
            }

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
            # Validate member exists before linking
            member = await self.get_member(member_id)
            if not member:
                logger.warning("INVITE_REJECTED: for_member=%s not found in members table", member_id)
                return None
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
            # Seed member profile with display_name from invite
            await self.upsert_member_profile(member_id, {"display_name": display_name})
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
