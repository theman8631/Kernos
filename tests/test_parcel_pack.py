"""Parcel Pillar 1 — pack validation and staging.

Spec reference: SPEC-PARCEL-PRIMITIVE-V1, Pillar 1 expected behaviors 1-8.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.parcel import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    ParcelService,
)


INSTANCE = "inst_parceltest"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    # Declare a relationship so pack's permission check passes.
    await idb.declare_relationship("alice", "bob", "full-access")
    svc = ParcelService(instance_db=idb, data_dir=str(tmp_path))
    yield svc, idb, tmp_path
    await idb.close()


def _space_dir(root: Path, instance_id: str, space_id: str) -> Path:
    from kernos.utils import _safe_name

    return root / _safe_name(instance_id) / "spaces" / space_id / "files"


@pytest.fixture
def alice_space(tmp_path):
    """Create alice's space and a couple of files."""
    space = _space_dir(tmp_path, INSTANCE, "sp_alice")
    space.mkdir(parents=True)
    (space / "a.txt").write_text("hello")
    (space / "b.txt").write_text("world")
    return space


class TestPackHappyPath:
    async def test_pack_with_valid_files_succeeds(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE,
            sender_member_id="alice",
            sender_space_id="sp_alice",
            recipient_member_id="bob",
            files=["a.txt", "b.txt"],
            note="two files",
        )
        assert result.ok is True
        assert result.status == "packed"
        assert result.file_count == 2
        assert result.total_bytes == len("hello") + len("world")
        assert result.parcel_id.startswith("parcel_")

    async def test_pack_creates_staging_dir(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["a.txt"],
        )
        parcel_dir = alice_space / "parcels" / result.parcel_id
        assert parcel_dir.is_dir()
        assert (parcel_dir / "a.txt").read_text() == "hello"
        assert (parcel_dir / "_manifest.json").is_file()

    async def test_manifest_records_sha256_and_sizes(self, env, alice_space):
        svc, idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["a.txt", "b.txt"],
        )
        row = await idb.get_parcel(result.parcel_id)
        manifest = json.loads(row["payload_manifest"])
        by_name = {e["filename"]: e for e in manifest}
        assert by_name["a.txt"]["size_bytes"] == len("hello")
        assert len(by_name["a.txt"]["sha256"]) == 64
        assert by_name["b.txt"]["size_bytes"] == len("world")


class TestPackRejections:
    async def test_rejects_missing_file(self, env, alice_space):
        svc, idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["does_not_exist.txt"],
        )
        assert result.ok is False
        assert "not found" in result.error
        # no row inserted
        rows = await idb.list_parcels(instance_id=INSTANCE, member_id="alice")
        assert rows == []

    async def test_rejects_outside_space_path(self, env, alice_space, tmp_path):
        svc, idb, _root = env
        (tmp_path / "outside.txt").write_text("leak")
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=[str(tmp_path / "outside.txt")],
        )
        assert result.ok is False
        assert "outside" in result.error
        rows = await idb.list_parcels(instance_id=INSTANCE, member_id="alice")
        assert rows == []

    async def test_rejects_dotdot_escape(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["../../../etc/passwd"],
        )
        assert result.ok is False
        assert "outside" in result.error

    async def test_rejects_unknown_recipient(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="ghost",
            files=["a.txt"],
        )
        assert result.ok is False
        assert "Unknown recipient" in result.error

    async def test_rejects_self_recipient(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="alice",
            files=["a.txt"],
        )
        assert result.ok is False
        assert "yourself" in result.error

    async def test_rejects_no_access_recipient(self, env, alice_space):
        svc, idb, _root = env
        # Default is "by-permission" (allows parcel); we must set "no-access"
        # explicitly to exercise the block path.
        await idb.create_member("charlie", "Charlie", "member", "")
        await idb.declare_relationship("alice", "charlie", "no-access")
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="charlie",
            files=["a.txt"],
        )
        assert result.ok is False
        assert "Permission denied" in result.error or "no-access" in result.error

    async def test_rejects_size_overflow(self, env, alice_space, monkeypatch):
        monkeypatch.setenv("KERNOS_PARCEL_MAX_BYTES", "10")
        svc, idb, _root = env
        # a.txt (5) + b.txt (5) = 10 bytes, within cap
        # Add one more byte to trigger
        (alice_space / "c.txt").write_text("x")
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["a.txt", "b.txt", "c.txt"],
        )
        assert result.ok is False
        assert "size cap" in result.error
        rows = await idb.list_parcels(instance_id=INSTANCE, member_id="alice")
        assert rows == []

    async def test_rejects_file_count_overflow(self, env, alice_space):
        svc, _idb, _root = env
        # Create 51 tiny files
        for i in range(51):
            (alice_space / f"f{i}.txt").write_text("x")
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=[f"f{i}.txt" for i in range(51)],
        )
        assert result.ok is False
        assert "file-count cap" in result.error

    async def test_empty_file_list_rejected(self, env, alice_space):
        svc, _idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=[],
        )
        assert result.ok is False


class TestPackRowPersistence:
    async def test_row_fields_populated(self, env, alice_space):
        svc, idb, _root = env
        result = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["a.txt"], note="here", ttl_days=3,
        )
        row = await idb.get_parcel(result.parcel_id)
        assert row["instance_id"] == INSTANCE
        assert row["sender_member_id"] == "alice"
        assert row["recipient_member_id"] == "bob"
        assert row["status"] == "packed"
        assert row["note"] == "here"
        assert row["ttl_days"] == 3
        assert row["total_bytes"] == 5
        assert row["sender_path"].endswith(
            os.path.join("sp_alice", "files", "parcels", result.parcel_id),
        )
        assert row["recipient_path"] == ""
        assert row["responded_at"] == ""
        assert row["delivered_at"] == ""
