"""Parcel Pillar 4 — list_parcels + inspect_parcel scoping.

Spec reference: SPEC-PARCEL-PRIMITIVE-V1, Pillar 4 expected behaviors 1-6.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.parcel import ParcelService
from kernos.utils import _safe_name


INSTANCE = "inst_audit"


def _space(root: Path, instance_id: str, space_id: str) -> Path:
    return root / _safe_name(instance_id) / "spaces" / space_id / "files"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    await idb.create_member("charlie", "Charlie", "member", "")
    await idb.declare_relationship("alice", "bob", "full-access")
    await idb.declare_relationship("alice", "charlie", "full-access")
    await idb.declare_relationship("bob", "alice", "full-access")

    svc = ParcelService(instance_db=idb, data_dir=str(tmp_path))
    alice_space = _space(tmp_path, INSTANCE, "sp_alice")
    alice_space.mkdir(parents=True)
    (alice_space / "a.txt").write_text("aaa")
    bob_space = _space(tmp_path, INSTANCE, "sp_bob")
    bob_space.mkdir(parents=True)
    (bob_space / "b.txt").write_text("bbb")
    yield svc, idb, tmp_path
    await idb.close()


async def _pack(svc, sender, sender_space, recipient, files):
    result = await svc.pack(
        instance_id=INSTANCE,
        sender_member_id=sender, sender_space_id=sender_space,
        recipient_member_id=recipient, files=files,
    )
    assert result.ok is True
    return result.parcel_id


class TestListParcelsScoping:
    async def test_sent_only_returns_sent(self, env):
        svc, idb, _root = env
        # Alice sends two; Bob sends one to Alice
        await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        await _pack(svc, "alice", "sp_alice", "charlie", ["a.txt"])
        await _pack(svc, "bob", "sp_bob", "alice", ["b.txt"])

        sent = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice", direction="sent",
        )
        assert len(sent) == 2
        assert all(p["sender_member_id"] == "alice" for p in sent)

    async def test_received_only_returns_received(self, env):
        svc, idb, _root = env
        await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        await _pack(svc, "bob", "sp_bob", "alice", ["b.txt"])

        recv = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice", direction="received",
        )
        assert len(recv) == 1
        assert recv[0]["recipient_member_id"] == "alice"

    async def test_all_direction_unions(self, env):
        svc, idb, _root = env
        await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        await _pack(svc, "bob", "sp_bob", "alice", ["b.txt"])

        all_p = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice", direction="all",
        )
        assert len(all_p) == 2

    async def test_status_filter(self, env):
        svc, idb, _root = env
        pid1 = await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        pid2 = await _pack(svc, "alice", "sp_alice", "charlie", ["a.txt"])
        # Mark pid1 delivered manually via DB
        await idb.update_parcel_status(pid1, "delivered")

        delivered = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice",
            direction="sent", status="delivered",
        )
        assert len(delivered) == 1
        assert delivered[0]["parcel_id"] == pid1

        packed = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice",
            direction="sent", status="packed",
        )
        assert len(packed) == 1
        assert packed[0]["parcel_id"] == pid2

    async def test_foreign_member_sees_no_parcels(self, env):
        svc, idb, _root = env
        await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        # Charlie is not involved
        foreign = await svc.list_for_member(
            instance_id=INSTANCE, member_id="charlie", direction="all",
        )
        assert foreign == []


class TestInspectParcel:
    async def test_inspect_returns_full_detail_for_sender(self, env):
        svc, _idb, _root = env
        pid = await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        detail = await svc.inspect(
            instance_id=INSTANCE, parcel_id=pid,
            requesting_member_id="alice",
        )
        assert detail is not None
        assert detail["parcel_id"] == pid
        assert detail["status"] == "packed"
        assert isinstance(detail["payload_manifest"], list)
        assert detail["payload_manifest"][0]["filename"] == "a.txt"
        assert len(detail["payload_manifest"][0]["sha256"]) == 64

    async def test_inspect_returns_detail_for_recipient(self, env):
        svc, _idb, _root = env
        pid = await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        detail = await svc.inspect(
            instance_id=INSTANCE, parcel_id=pid,
            requesting_member_id="bob",
        )
        assert detail is not None

    async def test_inspect_rejects_unknown_parcel(self, env):
        svc, _idb, _root = env
        detail = await svc.inspect(
            instance_id=INSTANCE, parcel_id="parcel_ghost",
            requesting_member_id="alice",
        )
        assert detail is None

    async def test_inspect_rejects_foreign_member(self, env):
        svc, _idb, _root = env
        pid = await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        detail = await svc.inspect(
            instance_id=INSTANCE, parcel_id=pid,
            requesting_member_id="charlie",
        )
        assert detail is None

    async def test_inspect_rejects_wrong_instance(self, env):
        svc, _idb, _root = env
        pid = await _pack(svc, "alice", "sp_alice", "bob", ["a.txt"])
        detail = await svc.inspect(
            instance_id="other_instance", parcel_id=pid,
            requesting_member_id="alice",
        )
        assert detail is None
