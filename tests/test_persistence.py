"""Tests for kernos/persistence (AC14–AC18).

Uses pytest's tmp_path fixture so no real files are left behind.
"""
from datetime import datetime, timezone

import pytest

from kernos.persistence import derive_tenant_id
from kernos.persistence.json_file import (
    JsonAuditStore,
    JsonConversationStore,
    JsonTenantStore,
)
from kernos.messages.models import AuthLevel, NormalizedMessage


# ---------------------------------------------------------------------------
# derive_tenant_id (AC18)
# ---------------------------------------------------------------------------


def _make_msg(platform: str = "sms", sender: str = "+15555550100") -> NormalizedMessage:
    return NormalizedMessage(
        content="hello",
        sender=sender,
        sender_auth_level=AuthLevel.owner_unverified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id="conv-1",
        timestamp=datetime.now(timezone.utc),
        tenant_id="",  # Empty so derive_tenant_id uses platform:sender fallback
    )


def test_derive_tenant_id_consistent():
    msg = _make_msg(platform="discord", sender="123456789")
    assert derive_tenant_id(msg) == "discord:123456789"
    assert derive_tenant_id(msg) == derive_tenant_id(msg)


def test_derive_tenant_id_different_platforms():
    sms_msg = _make_msg(platform="sms", sender="+15555550100")
    discord_msg = _make_msg(platform="discord", sender="+15555550100")
    assert derive_tenant_id(sms_msg) != derive_tenant_id(discord_msg)


def test_derive_tenant_id_different_senders():
    msg1 = _make_msg(sender="+15555550100")
    msg2 = _make_msg(sender="+15555550101")
    assert derive_tenant_id(msg1) != derive_tenant_id(msg2)


# ---------------------------------------------------------------------------
# ConversationStore (AC14, AC15, AC16, AC17)
# ---------------------------------------------------------------------------


async def test_get_recent_empty_for_new_conversation(tmp_path):
    """AC16: cold start returns empty list."""
    store = JsonConversationStore(tmp_path)
    result = await store.get_recent("tenant1", "conv1")
    assert result == []


async def test_append_and_get_recent(tmp_path):
    """AC14: append then get_recent returns entries oldest-first."""
    store = JsonConversationStore(tmp_path)
    await store.append(
        "tenant1",
        "conv1",
        {"role": "user", "content": "Hello", "timestamp": "2026-03-01T00:00:00Z",
         "platform": "sms", "tenant_id": "tenant1", "conversation_id": "conv1"},
    )
    await store.append(
        "tenant1",
        "conv1",
        {"role": "assistant", "content": "Hi there!", "timestamp": "2026-03-01T00:00:01Z",
         "platform": "sms", "tenant_id": "tenant1", "conversation_id": "conv1"},
    )
    result = await store.get_recent("tenant1", "conv1")
    assert len(result) == 2
    assert result[0] == {"role": "user", "content": "Hello"}
    assert result[1] == {"role": "assistant", "content": "Hi there!"}


async def test_get_recent_returns_only_role_and_content(tmp_path):
    """get_recent strips metadata — only role and content for Claude's messages array."""
    store = JsonConversationStore(tmp_path)
    await store.append(
        "tenant1",
        "conv1",
        {"role": "user", "content": "Test", "timestamp": "2026-03-01T00:00:00Z",
         "platform": "sms", "tenant_id": "tenant1", "conversation_id": "conv1"},
    )
    result = await store.get_recent("tenant1", "conv1")
    assert len(result) == 1
    assert set(result[0].keys()) == {"role", "content"}


async def test_get_recent_limit(tmp_path):
    """AC15: limit=20 returns only the 20 most recent when more exist."""
    store = JsonConversationStore(tmp_path)
    for i in range(25):
        await store.append(
            "tenant1",
            "conv1",
            {"role": "user", "content": f"msg {i}", "timestamp": "2026-03-01T00:00:00Z",
             "platform": "sms", "tenant_id": "tenant1", "conversation_id": "conv1"},
        )
    result = await store.get_recent("tenant1", "conv1", limit=20)
    assert len(result) == 20
    # Most recent 20: messages 5..24
    assert result[0]["content"] == "msg 5"
    assert result[-1]["content"] == "msg 24"


async def test_get_recent_respects_custom_limit(tmp_path):
    store = JsonConversationStore(tmp_path)
    for i in range(10):
        await store.append(
            "t1", "c1",
            {"role": "user", "content": f"msg {i}", "timestamp": "t", "platform": "sms",
             "tenant_id": "t1", "conversation_id": "c1"},
        )
    result = await store.get_recent("t1", "c1", limit=5)
    assert len(result) == 5
    assert result[-1]["content"] == "msg 9"


async def test_archive_moves_file_and_leaves_metadata(tmp_path):
    """AC17: archive() creates archive dir with metadata; original is gone."""
    store = JsonConversationStore(tmp_path)
    await store.append(
        "tenant1",
        "conv1",
        {"role": "user", "content": "Test", "timestamp": "2026-03-01T00:00:00Z",
         "platform": "sms", "tenant_id": "tenant1", "conversation_id": "conv1"},
    )

    # Verify the file exists before archiving
    conv_path = tmp_path / "tenant1" / "conversations" / "conv1.json"
    assert conv_path.exists()

    await store.archive("tenant1", "conv1")

    # Original is gone from active conversations
    assert not conv_path.exists()

    # Archive contains the file with metadata
    archive_root = tmp_path / "tenant1" / "archive" / "conversations"
    archive_dirs = list(archive_root.iterdir())
    assert len(archive_dirs) == 1  # one timestamped directory

    archived_file = list(archive_dirs[0].iterdir())[0]
    import json
    with open(archived_file) as f:
        archived = json.load(f)
    assert "archived_at" in archived
    assert archived["tenant_id"] == "tenant1"
    assert archived["conversation_id"] == "conv1"
    assert len(archived["entries"]) == 1


async def test_archive_noop_for_nonexistent_conversation(tmp_path):
    """archive() on a nonexistent conversation doesn't raise."""
    store = JsonConversationStore(tmp_path)
    await store.archive("tenant1", "nonexistent")  # must not raise


async def test_conversations_are_tenant_isolated(tmp_path):
    """Different tenant_ids produce separate stores."""
    store = JsonConversationStore(tmp_path)
    await store.append("tenant_a", "conv1",
                       {"role": "user", "content": "A's message", "timestamp": "t",
                        "platform": "sms", "tenant_id": "tenant_a", "conversation_id": "conv1"})
    await store.append("tenant_b", "conv1",
                       {"role": "user", "content": "B's message", "timestamp": "t",
                        "platform": "sms", "tenant_id": "tenant_b", "conversation_id": "conv1"})

    a_history = await store.get_recent("tenant_a", "conv1")
    b_history = await store.get_recent("tenant_b", "conv1")

    assert len(a_history) == 1
    assert a_history[0]["content"] == "A's message"
    assert len(b_history) == 1
    assert b_history[0]["content"] == "B's message"


# ---------------------------------------------------------------------------
# TenantStore (AC14)
# ---------------------------------------------------------------------------


async def test_get_or_create_creates_on_first_call(tmp_path):
    """AC14: get_or_create creates a tenant record for a new tenant."""
    store = JsonTenantStore(tmp_path)
    record = await store.get_or_create("discord:123456")
    assert record["tenant_id"] == "discord:123456"
    assert record["status"] == "active"
    assert "created_at" in record
    assert "capabilities" in record


async def test_get_or_create_returns_existing_on_second_call(tmp_path):
    """AC14: get_or_create returns the same record on subsequent calls."""
    store = JsonTenantStore(tmp_path)
    first = await store.get_or_create("discord:123456")
    second = await store.get_or_create("discord:123456")
    assert first["tenant_id"] == second["tenant_id"]
    assert first["created_at"] == second["created_at"]


async def test_get_or_create_creates_full_directory_structure(tmp_path):
    """get_or_create creates archive subdirs from day one."""
    store = JsonTenantStore(tmp_path)
    await store.get_or_create("discord:123456")

    tenant_root = tmp_path / "discord_123456"
    assert (tenant_root / "conversations").exists()
    assert (tenant_root / "audit").exists()
    archive = tenant_root / "archive"
    for subdir in ["conversations", "email", "files", "calendar", "contacts", "memory", "agents"]:
        assert (archive / subdir).exists(), f"archive/{subdir} missing"


async def test_tenant_save_and_reload(tmp_path):
    store = JsonTenantStore(tmp_path)
    record = await store.get_or_create("sms:+15555550100")
    record["capabilities"]["google-calendar"] = {"status": "connected"}
    await store.save("sms:+15555550100", record)

    reloaded = await store.get_or_create("sms:+15555550100")
    assert "google-calendar" in reloaded["capabilities"]


# ---------------------------------------------------------------------------
# AuditStore (AC14)
# ---------------------------------------------------------------------------


async def test_audit_log_creates_entry(tmp_path):
    """AC14: audit.log creates an entry."""
    store = JsonAuditStore(tmp_path)
    await store.log(
        "discord:123456",
        {
            "type": "tool_call",
            "timestamp": "2026-03-01T16:30:00Z",
            "tenant_id": "discord:123456",
            "conversation_id": "chan-1",
            "tool_name": "list_events",
            "tool_input": {"date": "2026-03-01"},
        },
    )

    # Find the audit file
    import json
    audit_dir = tmp_path / "discord_123456" / "audit"
    audit_files = list(audit_dir.iterdir())
    assert len(audit_files) == 1
    with open(audit_files[0]) as f:
        entries = json.load(f)
    assert len(entries) == 1
    assert entries[0]["type"] == "tool_call"
    assert entries[0]["tool_name"] == "list_events"


async def test_audit_log_appends_multiple_entries(tmp_path):
    store = JsonAuditStore(tmp_path)
    await store.log("t1", {"type": "tool_call", "timestamp": "t", "tenant_id": "t1",
                           "conversation_id": "c1", "tool_name": "foo", "tool_input": {}})
    await store.log("t1", {"type": "tool_result", "timestamp": "t", "tenant_id": "t1",
                           "conversation_id": "c1", "tool_name": "foo", "tool_output": "bar"})

    import json
    audit_dir = tmp_path / "t1" / "audit"
    audit_files = list(audit_dir.iterdir())
    with open(audit_files[0]) as f:
        entries = json.load(f)
    assert len(entries) == 2
