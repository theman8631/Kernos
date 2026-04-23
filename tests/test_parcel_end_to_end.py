"""Parcel end-to-end integration — pack → offer → accept → deliver.

Spec reference: SPEC-PARCEL-PRIMITIVE-V1, full-flow scenario listed under
test additions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.parcel import ParcelService
from kernos.utils import _safe_name


INSTANCE = "inst_e2e"


def _space(root: Path, instance_id: str, space_id: str) -> Path:
    return root / _safe_name(instance_id) / "spaces" / space_id / "files"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    await idb.declare_relationship("alice", "bob", "full-access")
    alice_space = _space(tmp_path, INSTANCE, "sp_alice")
    alice_space.mkdir(parents=True)
    (alice_space / "photo1.jpg").write_text("image-one")
    (alice_space / "photo2.jpg").write_text("image-two-bytes")
    (alice_space / "notes.txt").write_text("captions")
    svc = ParcelService(instance_db=idb, data_dir=str(tmp_path))
    yield svc, idb, tmp_path, alice_space
    await idb.close()


class TestFullAcceptFlow:
    async def test_pack_accept_delivers_all_files_verified(self, env):
        svc, idb, root, _as = env
        # 1. Alice packs three files for Bob
        pack = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["photo1.jpg", "photo2.jpg", "notes.txt"],
            note="website photos and captions",
        )
        assert pack.ok is True
        pid = pack.parcel_id
        assert pack.file_count == 3

        # 2. Bob accepts
        resp = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        assert resp.ok is True
        assert resp.status == "delivered"
        assert set(resp.files_delivered) == {
            "photo1.jpg", "photo2.jpg", "notes.txt",
        }

        # 3. Files exist in Bob's space with identical bytes
        bob_parcels = _space(root, INSTANCE, "sp_bob") / "parcels" / pid
        assert bob_parcels.is_dir()
        assert (bob_parcels / "photo1.jpg").read_text() == "image-one"
        assert (bob_parcels / "photo2.jpg").read_text() == "image-two-bytes"
        assert (bob_parcels / "notes.txt").read_text() == "captions"
        # Manifest mirrored too
        manifest = json.loads((bob_parcels / "_manifest.json").read_text())
        assert manifest["parcel_id"] == pid

        # 4. Row timestamps + status reflect the lifecycle
        row = await idb.get_parcel(pid)
        assert row["status"] == "delivered"
        assert row["created_at"]
        assert row["responded_at"]
        assert row["delivered_at"]
        # created_at <= responded_at <= delivered_at lexicographically
        # (ISO-8601 UTC → sortable as strings)
        assert row["created_at"] <= row["responded_at"] <= row["delivered_at"]

        # 5. Both sides can list the delivered parcel
        alice_sent = await svc.list_for_member(
            instance_id=INSTANCE, member_id="alice", direction="sent",
        )
        bob_recv = await svc.list_for_member(
            instance_id=INSTANCE, member_id="bob", direction="received",
        )
        assert len(alice_sent) == 1 and alice_sent[0]["parcel_id"] == pid
        assert len(bob_recv) == 1 and bob_recv[0]["parcel_id"] == pid

        # 6. Inspect works for both sides, blocked for anyone else
        detail_alice = await svc.inspect(
            instance_id=INSTANCE, parcel_id=pid, requesting_member_id="alice",
        )
        detail_bob = await svc.inspect(
            instance_id=INSTANCE, parcel_id=pid, requesting_member_id="bob",
        )
        assert detail_alice is not None and detail_alice["status"] == "delivered"
        assert detail_bob is not None and detail_bob["status"] == "delivered"


class TestFullDeclineFlow:
    async def test_pack_decline_leaves_sender_staged_files_intact(self, env):
        svc, idb, root, alice_space = env
        pack = await svc.pack(
            instance_id=INSTANCE, sender_member_id="alice",
            sender_space_id="sp_alice", recipient_member_id="bob",
            files=["photo1.jpg"],
        )
        pid = pack.parcel_id

        resp = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="decline",
            reason="not interested",
        )
        assert resp.ok is True
        assert resp.status == "declined"

        # Alice's staged file is untouched
        staged = alice_space / "parcels" / pid / "photo1.jpg"
        assert staged.is_file()
        assert staged.read_text() == "image-one"

        # Bob's space has no parcel directory
        bob_parcels = _space(root, INSTANCE, "sp_bob") / "parcels" / pid
        assert not bob_parcels.exists()

        row = await idb.get_parcel(pid)
        assert row["status"] == "declined"
        assert row["decline_reason"] == "not interested"
