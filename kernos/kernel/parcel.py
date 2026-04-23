"""Parcel primitive — cross-space file transfer between members (PARCEL-PRIMITIVE-V1).

A parcel moves one or more files from a sender member's active space to a
recipient member's space with a consented, auditable lifecycle:

    pack_parcel()     → status=packed       → RM offer dispatched
    respond_to_parcel(accept) → files copied + sha256 verified → status=delivered
    respond_to_parcel(decline) → status=declined, sender notified
    TTL elapses       → status=expired (via expire_stale_parcels)

Both members live on the same Kernos instance sharing one filesystem, so
"transfer" is a copy, not a network move. The value isn't encryption or
replication; it's a *coordinated, consented* exchange the agent can
reason about without hallucinating a tool call that doesn't exist.

Size cap: ``KERNOS_PARCEL_MAX_BYTES`` (default 100 MB total).
File-count cap: 50 per parcel. Both are structural guards against
accidental whole-directory packs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kernos.utils import _safe_name, utc_now

logger = logging.getLogger(__name__)


DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_FILES = 50
DEFAULT_TTL_DAYS = 7

#: Valid parcel lifecycle states.
STATES = ("packed", "accepted", "delivered", "declined", "expired", "failed")


def _get_max_bytes() -> int:
    raw = os.getenv("KERNOS_PARCEL_MAX_BYTES", "")
    try:
        if raw:
            return int(raw)
    except ValueError:
        logger.warning(
            "KERNOS_PARCEL_MAX_BYTES invalid %r — defaulting to %d",
            raw, DEFAULT_MAX_BYTES,
        )
    return DEFAULT_MAX_BYTES


def _generate_parcel_id() -> str:
    return f"parcel_{uuid.uuid4().hex[:12]}"


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_space_dir(data_dir: str, instance_id: str, space_id: str) -> str:
    return str(
        Path(data_dir) / _safe_name(instance_id) / "spaces" / space_id / "files"
    )


def _resolve_parcel_dir(space_dir: str, parcel_id: str) -> str:
    return os.path.join(space_dir, "parcels", parcel_id)


# ---------------------------------------------------------------------------
# Result dataclasses — structured returns for the kernel tools
# ---------------------------------------------------------------------------


@dataclass
class ParcelPackResult:
    ok: bool
    parcel_id: str = ""
    status: str = ""
    total_bytes: int = 0
    file_count: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "parcel_id": self.parcel_id,
            "status": self.status,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class ParcelResponseResult:
    ok: bool
    parcel_id: str = ""
    status: str = ""
    files_delivered: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "parcel_id": self.parcel_id,
            "status": self.status,
            "files_delivered": list(self.files_delivered),
        }
        if self.error:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------
# ParcelService
# ---------------------------------------------------------------------------


class ParcelService:
    """Orchestrates pack / respond / list / inspect over the parcels table
    and the on-disk ``parcels/`` convention.

    Does not directly dispatch the parcel-offer RM envelope — the caller
    (the handler) invokes the relational dispatcher after ``pack`` succeeds
    so the envelope and the DB row stay transactionally independent.
    """

    def __init__(
        self,
        instance_db: Any,
        data_dir: str = "./data",
    ) -> None:
        self._db = instance_db
        self._data_dir = data_dir

    # ---- Pack ------------------------------------------------------------

    async def pack(
        self,
        *,
        instance_id: str,
        sender_member_id: str,
        sender_space_id: str,
        recipient_member_id: str,
        files: list[str],
        note: str = "",
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> ParcelPackResult:
        # 1. Basic validation
        if not files:
            return ParcelPackResult(ok=False, error="No files specified.")
        if len(files) > DEFAULT_MAX_FILES:
            return ParcelPackResult(
                ok=False,
                error=(
                    f"Parcel exceeds file-count cap ({len(files)} > {DEFAULT_MAX_FILES})."
                ),
            )
        if not recipient_member_id:
            return ParcelPackResult(ok=False, error="recipient_member_id is required.")
        if recipient_member_id == sender_member_id:
            return ParcelPackResult(
                ok=False, error="Cannot send a parcel to yourself.",
            )

        # 2. Recipient must exist
        recipient = await self._db.get_member(recipient_member_id)
        if not recipient:
            return ParcelPackResult(
                ok=False,
                error=f"Unknown recipient_member_id {recipient_member_id!r}.",
            )

        # 3. Permission check (sender's side toward recipient). The
        # relationship enum is {'full-access', 'no-access', 'by-permission'};
        # 'no-access' is the only state that blocks a parcel offer at Layer 1.
        # The Messenger cohort (Layer 2) may still decline, but that's
        # evaluated on offer dispatch, not here.
        try:
            perm = await self._db.get_permission(
                sender_member_id, recipient_member_id,
            )
        except Exception as exc:
            logger.warning("PARCEL_PACK_PERMISSION_LOOKUP_FAILED: %s", exc)
            perm = "no-access"
        if perm == "no-access":
            return ParcelPackResult(
                ok=False,
                error=(
                    f"Permission denied: no-access declared from sender to "
                    f"{recipient_member_id}; no parcel offer can be sent."
                ),
            )

        # 4. Resolve sender space + validate each file is inside it
        sender_space_dir = _resolve_space_dir(
            self._data_dir, instance_id, sender_space_id,
        )
        os.makedirs(sender_space_dir, exist_ok=True)
        space_abs = os.path.realpath(sender_space_dir)

        resolved_files: list[tuple[str, str]] = []  # (absolute_src_path, basename)
        for f in files:
            if not f or not isinstance(f, str):
                return ParcelPackResult(ok=False, error=f"Invalid file entry {f!r}.")
            if os.path.isabs(f):
                src = os.path.realpath(f)
            else:
                src = os.path.realpath(os.path.join(sender_space_dir, f))
            if not (src == space_abs or src.startswith(space_abs + os.sep)):
                return ParcelPackResult(
                    ok=False,
                    error=f"File {f!r} resolves outside the active space.",
                )
            if not os.path.isfile(src):
                return ParcelPackResult(
                    ok=False, error=f"File not found: {f!r}.",
                )
            resolved_files.append((src, os.path.basename(src)))

        # 5. Compute manifest + check size cap
        max_bytes = _get_max_bytes()
        manifest: list[dict[str, Any]] = []
        total_bytes = 0
        for src, name in resolved_files:
            size = os.path.getsize(src)
            total_bytes += size
            if total_bytes > max_bytes:
                return ParcelPackResult(
                    ok=False,
                    error=(
                        f"Parcel exceeds size cap ({total_bytes} > {max_bytes} bytes)."
                    ),
                )
            manifest.append({
                "filename": name,
                "size_bytes": size,
                "sha256": _sha256_of_file(src),
            })

        # 6. Stage files under {sender_space}/parcels/{parcel_id}/
        parcel_id = _generate_parcel_id()
        parcel_dir = _resolve_parcel_dir(sender_space_dir, parcel_id)
        os.makedirs(parcel_dir, exist_ok=True)
        for src, name in resolved_files:
            shutil.copyfile(src, os.path.join(parcel_dir, name))
        # Mirror manifest for agent-readable inspection without hitting the DB
        with open(
            os.path.join(parcel_dir, "_manifest.json"), "w", encoding="utf-8",
        ) as mf:
            json.dump({
                "parcel_id": parcel_id,
                "sender_member_id": sender_member_id,
                "recipient_member_id": recipient_member_id,
                "note": note,
                "total_bytes": total_bytes,
                "files": manifest,
            }, mf, indent=2)

        # 7. Persist the parcel row
        now = utc_now()
        row = {
            "parcel_id": parcel_id,
            "instance_id": instance_id,
            "sender_member_id": sender_member_id,
            "recipient_member_id": recipient_member_id,
            "status": "packed",
            "payload_manifest": json.dumps(manifest),
            "total_bytes": total_bytes,
            "note": note,
            "ttl_days": int(ttl_days or DEFAULT_TTL_DAYS),
            "sender_path": parcel_dir,
            "recipient_path": "",
            "decline_reason": "",
            "created_at": now,
            "responded_at": "",
            "delivered_at": "",
            "expired_at": "",
        }
        await self._db.save_parcel(row)

        logger.info(
            "PARCEL_PACKED: instance=%s parcel=%s sender=%s recipient=%s "
            "files=%d total_bytes=%d",
            instance_id, parcel_id, sender_member_id, recipient_member_id,
            len(manifest), total_bytes,
        )
        return ParcelPackResult(
            ok=True,
            parcel_id=parcel_id,
            status="packed",
            total_bytes=total_bytes,
            file_count=len(manifest),
        )

    # ---- Respond ---------------------------------------------------------

    async def respond(
        self,
        *,
        instance_id: str,
        parcel_id: str,
        responder_member_id: str,
        action: str,                 # "accept" | "decline"
        reason: str = "",
        recipient_space_id: str = "",
    ) -> ParcelResponseResult:
        if action not in ("accept", "decline"):
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id,
                error=f"Invalid action {action!r}; expected 'accept' or 'decline'.",
            )

        parcel = await self._db.get_parcel(parcel_id)
        if not parcel:
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id, error="Unknown parcel_id.",
            )
        if parcel["instance_id"] != instance_id:
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id,
                error="Parcel does not belong to this instance.",
            )
        if parcel["recipient_member_id"] != responder_member_id:
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id,
                error="Only the recipient can respond to a parcel.",
            )
        if parcel["status"] != "packed":
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id, status=parcel["status"],
                error=(
                    f"Parcel is already {parcel['status']!r}; no further "
                    "responses accepted."
                ),
            )

        now = utc_now()

        if action == "decline":
            await self._db.update_parcel_status(
                parcel_id, "declined",
                responded_at=now, decline_reason=reason or "",
            )
            logger.info(
                "PARCEL_DECLINED: instance=%s parcel=%s reason=%r",
                instance_id, parcel_id, reason or "",
            )
            return ParcelResponseResult(
                ok=True, parcel_id=parcel_id, status="declined",
            )

        # accept path
        if not recipient_space_id:
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id,
                error="recipient_space_id is required on accept.",
            )

        await self._db.update_parcel_status(
            parcel_id, "accepted", responded_at=now,
        )
        logger.info(
            "PARCEL_ACCEPTED: instance=%s parcel=%s", instance_id, parcel_id,
        )

        recipient_space_dir = _resolve_space_dir(
            self._data_dir, instance_id, recipient_space_id,
        )
        os.makedirs(recipient_space_dir, exist_ok=True)
        recipient_parcel_dir = _resolve_parcel_dir(recipient_space_dir, parcel_id)
        os.makedirs(recipient_parcel_dir, exist_ok=True)

        manifest = json.loads(parcel.get("payload_manifest", "[]") or "[]")
        sender_path = parcel.get("sender_path", "")
        if not sender_path or not os.path.isdir(sender_path):
            await self._db.update_parcel_status(parcel_id, "failed")
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id, status="failed",
                error="Sender staging directory missing; parcel cannot be delivered.",
            )

        delivered: list[str] = []
        try:
            for entry in manifest:
                name = entry["filename"]
                src = os.path.join(sender_path, name)
                dst = os.path.join(recipient_parcel_dir, name)
                shutil.copyfile(src, dst)
                actual = _sha256_of_file(dst)
                if actual != entry["sha256"]:
                    raise ValueError(
                        f"sha256 mismatch on {name}: expected "
                        f"{entry['sha256']}, got {actual}"
                    )
                delivered.append(name)
            # Mirror manifest into recipient's parcel dir too
            shutil.copyfile(
                os.path.join(sender_path, "_manifest.json"),
                os.path.join(recipient_parcel_dir, "_manifest.json"),
            )
        except Exception as exc:
            logger.warning(
                "PARCEL_FAILED: parcel=%s error=%s", parcel_id, exc,
            )
            # Roll back: remove whatever landed in the recipient's dir
            try:
                shutil.rmtree(recipient_parcel_dir, ignore_errors=True)
            except Exception:
                pass
            await self._db.update_parcel_status(parcel_id, "failed")
            return ParcelResponseResult(
                ok=False, parcel_id=parcel_id, status="failed",
                error=f"Transfer failed: {exc}",
            )

        await self._db.update_parcel_status(
            parcel_id, "delivered",
            delivered_at=utc_now(),
            recipient_path=recipient_parcel_dir,
        )
        logger.info(
            "PARCEL_DELIVERED: instance=%s parcel=%s files=%d",
            instance_id, parcel_id, len(delivered),
        )
        return ParcelResponseResult(
            ok=True, parcel_id=parcel_id, status="delivered",
            files_delivered=delivered,
        )

    # ---- Audit -----------------------------------------------------------

    async def list_for_member(
        self,
        *,
        instance_id: str,
        member_id: str,
        direction: str = "all",
        status: str = "all",
    ) -> list[dict]:
        return await self._db.list_parcels(
            instance_id=instance_id,
            member_id=member_id,
            direction=direction,
            status=status,
        )

    async def inspect(
        self,
        *,
        instance_id: str,
        parcel_id: str,
        requesting_member_id: str,
    ) -> dict | None:
        """Return the parcel detail, or None if not-found / not-scoped.

        Callers should treat None as a not-found-or-forbidden signal and
        return a user-facing error; this method deliberately conflates the
        two so members can't use inspect to probe for other members'
        parcel ids.
        """
        parcel = await self._db.get_parcel(parcel_id)
        if not parcel:
            return None
        if parcel.get("instance_id") != instance_id:
            return None
        if requesting_member_id not in (
            parcel.get("sender_member_id"),
            parcel.get("recipient_member_id"),
        ):
            return None
        # Expand manifest JSON for the caller
        out = dict(parcel)
        try:
            out["payload_manifest"] = json.loads(parcel.get("payload_manifest", "[]") or "[]")
        except Exception:
            out["payload_manifest"] = []
        return out

    # ---- Expiry ----------------------------------------------------------

    async def expire_stale(self) -> int:
        n = await self._db.expire_stale_parcels(now_iso=utc_now())
        if n:
            logger.info("PARCEL_EXPIRED: count=%d", n)
        return n
