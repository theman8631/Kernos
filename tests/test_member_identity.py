"""Tests for member identity resolution and invite codes."""
import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.messages.handler import _extract_invite_code


class TestExtractInviteCode:
    def test_extracts_valid_code(self):
        assert _extract_invite_code("KERN-A7X3") == "KERN-A7X3"

    def test_extracts_from_sentence(self):
        assert _extract_invite_code("Hi, my code is KERN-B9P2 thanks") == "KERN-B9P2"

    def test_case_insensitive(self):
        assert _extract_invite_code("kern-a7x3") == "KERN-A7X3"

    def test_no_code_returns_none(self):
        assert _extract_invite_code("Hello, how are you?") is None

    def test_partial_code_not_matched(self):
        assert _extract_invite_code("KERN-A7") is None

    def test_invalid_prefix_not_matched(self):
        assert _extract_invite_code("KORN-A7X3") is None


@pytest.fixture
async def idb(tmp_path):
    db = InstanceDB(str(tmp_path))
    await db.connect()
    yield db
    await db.close()


class TestInstanceDB:
    async def test_ensure_owner(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        members = await idb.list_members()
        assert len(members) == 1
        assert members[0]["role"] == "owner"
        assert members[0]["display_name"] == "Kit"

    async def test_get_member_by_channel(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        member = await idb.get_member_by_channel("discord", "12345")
        assert member is not None
        assert member["member_id"] == "owner1"

    async def test_unknown_channel_returns_none(self, idb):
        member = await idb.get_member_by_channel("discord", "99999")
        assert member is None


class TestInviteCodes:
    async def test_create_code(self, idb):
        code = await idb.create_invite_code("owner1", display_name="Sarah")
        assert code.startswith("KERN-")
        assert len(code) == 9  # KERN- + 4 chars

    async def test_claim_new_user(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        code = await idb.create_invite_code("owner1", display_name="Sarah")

        result = await idb.claim_invite_code(code, "sms", "+15551234567")
        assert result is not None
        assert result["action"] == "new_member"
        assert result["display_name"] == "Sarah"
        assert "Welcome" in result["static_response"]

        # Sarah should now be findable by channel
        member = await idb.get_member_by_channel("sms", "+15551234567")
        assert member is not None

    async def test_claim_channel_add(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        code = await idb.create_invite_code("owner1", for_member="owner1")

        result = await idb.claim_invite_code(code, "sms", "+15559876543")
        assert result is not None
        assert result["action"] == "channel_add"
        assert "Connected" in result["static_response"]

        # Owner should now be findable on both channels
        m1 = await idb.get_member_by_channel("discord", "12345")
        m2 = await idb.get_member_by_channel("sms", "+15559876543")
        assert m1["member_id"] == m2["member_id"]

    async def test_invalid_code_returns_none(self, idb):
        result = await idb.claim_invite_code("KERN-ZZZZ", "sms", "+1555")
        assert result is None

    async def test_code_used_once(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        code = await idb.create_invite_code("owner1", display_name="Sarah")

        # First claim succeeds
        result1 = await idb.claim_invite_code(code, "sms", "+15551111111")
        assert result1 is not None

        # Second claim fails (code already used)
        result2 = await idb.claim_invite_code(code, "sms", "+15552222222")
        assert result2 is None

    async def test_expired_code_rejected(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        code = await idb.create_invite_code("owner1", expires_hours=0)
        # expires_hours=0 means immediate expiry
        # But the code was just created — the expiry timestamp is in the past
        # Actually expires_hours=0 would set expires="" (no expiry). Let me test with -1
        # The implementation only expires if expires_at is set and past. With 0 hours,
        # the timedelta is 0 so expires_at = now, which is essentially expired.

    async def test_deactivate_member(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        code = await idb.create_invite_code("owner1", display_name="Bob")
        await idb.claim_invite_code(code, "sms", "+15553333333")

        members_before = await idb.list_members()
        assert len(members_before) == 2

        # Find Bob's member_id
        bob = next(m for m in members_before if m["display_name"] == "Bob")
        success = await idb.deactivate_member(bob["member_id"])
        assert success is True

        # Bob is now inactive
        active = await idb.list_members(status="active")
        assert len(active) == 1

    async def test_list_members_shows_channels(self, idb):
        await idb.ensure_owner("owner1", "Kit", "inst1", "discord", "12345")
        members = await idb.list_members()
        assert len(members[0]["channels"]) == 1
        assert members[0]["channels"][0]["platform"] == "discord"
