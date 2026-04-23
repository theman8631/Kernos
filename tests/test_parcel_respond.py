"""Parcel Pillar 3 — accept / decline lifecycle + sha256 verification.

Spec reference: SPEC-PARCEL-PRIMITIVE-V1, Pillar 3 expected behaviors 1-6.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.parcel import ParcelService
from kernos.utils import _safe_name, utc_now


INSTANCE = "inst_respond"


def _space(root: Path, instance_id: str, space_id: str) -> Path:
    return root / _safe_name(instance_id) / "spaces" / space_id / "files"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    await idb.declare_relationship("alice", "bob", "full-access")
    svc = ParcelService(instance_db=idb, data_dir=str(tmp_path))
    alice_space = _space(tmp_path, INSTANCE, "sp_alice")
    alice_space.mkdir(parents=True)
    (alice_space / "photo.jpg").write_text("image-bytes")
    yield svc, idb, tmp_path, alice_space
    await idb.close()


async def _pack(svc, files=None) -> str:
    result = await svc.pack(
        instance_id=INSTANCE,
        sender_member_id="alice",
        sender_space_id="sp_alice",
        recipient_member_id="bob",
        files=files or ["photo.jpg"],
    )
    assert result.ok is True
    return result.parcel_id


class TestAccept:
    async def test_accept_copies_files_into_recipient_space(self, env):
        svc, idb, root, _as = env
        pid = await _pack(svc)
        result = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        assert result.ok is True
        assert result.status == "delivered"
        assert result.files_delivered == ["photo.jpg"]
        dst = _space(root, INSTANCE, "sp_bob") / "parcels" / pid / "photo.jpg"
        assert dst.is_file()
        assert dst.read_text() == "image-bytes"

    async def test_accept_transitions_status_to_delivered(self, env):
        svc, idb, _root, _as = env
        pid = await _pack(svc)
        await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        row = await idb.get_parcel(pid)
        assert row["status"] == "delivered"
        assert row["delivered_at"]
        assert row["recipient_path"]

    async def test_accept_verifies_sha256(self, env):
        svc, idb, root, alice_space = env
        pid = await _pack(svc)
        # Tamper the sender's staged file AFTER packing
        parcel_dir = alice_space / "parcels" / pid
        (parcel_dir / "photo.jpg").write_text("TAMPERED")
        result = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        assert result.ok is False
        assert result.status == "failed"
        assert "sha256" in (result.error or "").lower() or "failed" in (result.error or "").lower()
        # Recipient parcel dir should have been rolled back
        recipient_dir = _space(root, INSTANCE, "sp_bob") / "parcels" / pid
        assert not recipient_dir.exists()
        row = await idb.get_parcel(pid)
        assert row["status"] == "failed"

    async def test_accept_by_non_recipient_rejected(self, env):
        svc, idb, _root, _as = env
        await idb.create_member("charlie", "Charlie", "member", "")
        pid = await _pack(svc)
        result = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="charlie", action="accept",
            recipient_space_id="sp_charlie",
        )
        assert result.ok is False
        assert "recipient" in (result.error or "").lower()


class TestDecline:
    async def test_decline_leaves_sender_files_alone(self, env):
        svc, idb, _root, alice_space = env
        pid = await _pack(svc)
        parcel_dir = alice_space / "parcels" / pid
        assert (parcel_dir / "photo.jpg").is_file()
        result = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="decline",
            reason="not now",
        )
        assert result.ok is True
        assert result.status == "declined"
        assert (parcel_dir / "photo.jpg").is_file()
        assert (parcel_dir / "photo.jpg").read_text() == "image-bytes"

    async def test_decline_records_reason(self, env):
        svc, idb, _root, _as = env
        pid = await _pack(svc)
        await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="decline",
            reason="too large for now",
        )
        row = await idb.get_parcel(pid)
        assert row["status"] == "declined"
        assert row["decline_reason"] == "too large for now"
        assert row["responded_at"]


class TestDoubleResponse:
    async def test_already_responded_rejects_further_response(self, env):
        svc, _idb, _root, _as = env
        pid = await _pack(svc)
        r1 = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="decline",
        )
        assert r1.ok is True
        r2 = await svc.respond(
            instance_id=INSTANCE, parcel_id=pid,
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        assert r2.ok is False
        assert "already" in (r2.error or "").lower()

    async def test_unknown_parcel_id_rejected(self, env):
        svc, _idb, _root, _as = env
        result = await svc.respond(
            instance_id=INSTANCE, parcel_id="parcel_fake",
            responder_member_id="bob", action="accept",
            recipient_space_id="sp_bob",
        )
        assert result.ok is False
        assert "Unknown" in (result.error or "")


class TestExpiry:
    async def test_expiry_sweep_transitions_stale_parcels(self, env):
        svc, idb, _root, alice_space = env
        # Pack with default ttl=7 days
        pid = await _pack(svc)
        # Back-date the created_at so TTL has elapsed
        from datetime import datetime, timezone, timedelta
        back = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        await idb._conn.execute(
            "UPDATE parcels SET created_at=? WHERE parcel_id=?",
            (back, pid),
        )
        await idb._conn.commit()

        n = await svc.expire_stale()
        assert n == 1
        row = await idb.get_parcel(pid)
        assert row["status"] == "expired"
        assert row["expired_at"]
