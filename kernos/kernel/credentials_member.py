"""Per-member credentials store for the workshop external-service primitive.

This is the AppData-pattern credentials surface: keyed by
(member_id, service_id), encrypted at rest, scoped at retrieval to the
invoking member's runtime context. Distinct from the install-level
credentials surface in kernos/kernel/credentials.py, which holds
Kernos-itself dependencies (LLM provider tokens, search API keys,
Google OAuth credential path for the calendar capability). The two
surfaces compose; neither subsumes the other.

Storage layout under the install's data directory:

    data/
    ├─ <instance>/
    │  ├─ credentials/
    │  │  ├─ <member_id>/
    │  │  │  └─ <service_id>.enc.json
    │  │  └─ .key                         # auto-generated if no env var
    │  └─ ...

Encryption uses Fernet (cryptography library) — symmetric AEAD with
authentication tags. The key is resolved in this order:

1. KERNOS_CREDENTIAL_KEY environment variable (operator-supplied,
   urlsafe base64 32-byte key).
2. <data_dir>/<instance>/credentials/.key file (mode 0600,
   auto-generated on first call if absent and no env var present).

On first auto-generation, a one-line notice prints reminding the
operator to back the key up or set it explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


_BACKUP_NOTICE = (
    "CREDENTIAL_KEY_GENERATED: a credential encryption key was generated "
    "at %s (mode 0600). Back this file up or set KERNOS_CREDENTIAL_KEY "
    "explicitly to override. Without the key, stored credentials cannot "
    "be recovered."
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredCredential:
    """Decrypted form of a single stored credential.

    Fields beyond `service_id` and `token` are optional and present for
    services that expose them through their auth flow. Audit treats
    `token` and `refresh_token` as opaque secrets — they never appear
    in audit-log payload digests.
    """

    service_id: str
    member_id: str
    token: str
    refresh_token: str = ""
    expires_at: int | None = None       # epoch seconds, None = no expiry
    scopes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    added_at: int = 0                    # epoch seconds
    rotated_at: int = 0                  # epoch seconds

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= int(time.time())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_name(value: str) -> str:
    """Filesystem-safe name per the same convention used elsewhere in Kernos."""
    if not value or value.strip() != value:
        raise ValueError(f"Invalid identifier: {value!r}")
    if not _VALID_NAME_RE.match(value):
        # Replace common platform-prefix separators (e.g. discord:1234 → discord_1234)
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
        if not cleaned or not _VALID_NAME_RE.match(cleaned):
            raise ValueError(f"Identifier {value!r} cannot be made safe")
        return cleaned
    return value


def _credentials_dir(data_dir: str | Path, instance_id: str) -> Path:
    return Path(data_dir) / _safe_name(instance_id) / "credentials"


def _member_dir(data_dir: str | Path, instance_id: str, member_id: str) -> Path:
    return _credentials_dir(data_dir, instance_id) / _safe_name(member_id)


def _credential_path(
    data_dir: str | Path, instance_id: str, member_id: str, service_id: str,
) -> Path:
    return _member_dir(data_dir, instance_id, member_id) / f"{_safe_name(service_id)}.enc.json"


def _key_path(data_dir: str | Path, instance_id: str) -> Path:
    return _credentials_dir(data_dir, instance_id) / ".key"


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def _resolve_key(data_dir: str | Path, instance_id: str) -> bytes:
    """Resolve the Fernet key.

    Order: env var → key file → auto-generate to key file with notice.
    """
    env_value = os.environ.get("KERNOS_CREDENTIAL_KEY", "").strip()
    if env_value:
        return env_value.encode("utf-8")

    path = _key_path(data_dir, instance_id)
    if path.exists():
        try:
            return path.read_bytes().strip()
        except Exception as exc:
            raise RuntimeError(f"Failed to read credential key at {path}: {exc}") from exc

    # First-run auto-generation. Mode 0600 set explicitly after write.
    path.parent.mkdir(parents=True, exist_ok=True)
    new_key = Fernet.generate_key()
    # Write the key with restrictive permissions from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, new_key)
    finally:
        os.close(fd)
    logger.warning(_BACKUP_NOTICE, path)
    return new_key


def _fernet_for(data_dir: str | Path, instance_id: str) -> Fernet:
    key = _resolve_key(data_dir, instance_id)
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"KERNOS_CREDENTIAL_KEY is not a valid Fernet key (must be 32 "
            f"url-safe base64-encoded bytes). Underlying error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# MemberCredentialStore
# ---------------------------------------------------------------------------


class MemberCredentialNotFound(KeyError):
    """Raised when a credential lookup misses for the (member, service) pair."""


class MemberCredentialStore:
    """Per-member credentials store.

    All operations are member-scoped: every method takes member_id and
    operates only against that member's directory under the install's
    credentials root.
    """

    def __init__(self, data_dir: str | Path, instance_id: str) -> None:
        self._data_dir = Path(data_dir)
        self._instance_id = instance_id

    # --- write path ---

    def add(
        self,
        *,
        member_id: str,
        service_id: str,
        token: str,
        refresh_token: str = "",
        expires_at: int | None = None,
        scopes: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> StoredCredential:
        """Store a credential. Replaces any existing credential for the pair.

        Use rotate() to update only token / refresh_token / expires_at
        while preserving added_at and metadata.
        """
        path = _credential_path(self._data_dir, self._instance_id, member_id, service_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        record = StoredCredential(
            service_id=service_id,
            member_id=member_id,
            token=token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=tuple(scopes or ()),
            metadata=dict(metadata or {}),
            added_at=now,
            rotated_at=now,
        )
        self._write(path, record)
        logger.info(
            "MEMBER_CREDENTIAL_ADD: instance=%s member=%s service=%s",
            self._instance_id, member_id, service_id,
        )
        return record

    def rotate(
        self,
        *,
        member_id: str,
        service_id: str,
        token: str,
        refresh_token: str | None = None,
        expires_at: int | None = None,
    ) -> StoredCredential:
        """Update token / refresh_token / expires_at in place. Preserves
        added_at, scopes, and metadata.
        """
        existing = self.get(member_id=member_id, service_id=service_id)
        path = _credential_path(self._data_dir, self._instance_id, member_id, service_id)
        record = StoredCredential(
            service_id=existing.service_id,
            member_id=existing.member_id,
            token=token,
            refresh_token=refresh_token if refresh_token is not None else existing.refresh_token,
            expires_at=expires_at if expires_at is not None else existing.expires_at,
            scopes=existing.scopes,
            metadata=existing.metadata,
            added_at=existing.added_at,
            rotated_at=int(time.time()),
        )
        self._write(path, record)
        logger.info(
            "MEMBER_CREDENTIAL_ROTATE: instance=%s member=%s service=%s",
            self._instance_id, member_id, service_id,
        )
        return record

    def revoke(self, *, member_id: str, service_id: str) -> bool:
        """Delete the local copy. Returns True if a credential was removed.

        Server-side revocation is the operator's responsibility at the
        service; this only purges Kernos's copy.
        """
        path = _credential_path(self._data_dir, self._instance_id, member_id, service_id)
        if not path.exists():
            return False
        try:
            path.unlink()
        except Exception as exc:
            logger.warning(
                "MEMBER_CREDENTIAL_REVOKE_FAILED: %s service=%s: %s",
                member_id, service_id, exc,
            )
            return False
        logger.info(
            "MEMBER_CREDENTIAL_REVOKE: instance=%s member=%s service=%s",
            self._instance_id, member_id, service_id,
        )
        return True

    # --- read path ---

    def get(self, *, member_id: str, service_id: str) -> StoredCredential:
        """Return the credential for (member, service). Raises
        MemberCredentialNotFound if absent.
        """
        path = _credential_path(self._data_dir, self._instance_id, member_id, service_id)
        if not path.exists():
            raise MemberCredentialNotFound(
                f"No credential for member={member_id} service={service_id}"
            )
        return self._read(path)

    def has(self, *, member_id: str, service_id: str) -> bool:
        """True if a credential exists for the pair (regardless of expiry)."""
        return _credential_path(
            self._data_dir, self._instance_id, member_id, service_id,
        ).exists()

    def list_services_for_member(self, member_id: str) -> list[str]:
        """Return the service ids the member has credentials for."""
        member_root = _member_dir(self._data_dir, self._instance_id, member_id)
        if not member_root.exists():
            return []
        out: list[str] = []
        for path in sorted(member_root.iterdir()):
            if path.suffix == ".json" and path.name.endswith(".enc.json"):
                out.append(path.name[: -len(".enc.json")])
        return out

    # --- internals ---

    def _write(self, path: Path, record: StoredCredential) -> None:
        fernet = _fernet_for(self._data_dir, self._instance_id)
        plaintext = json.dumps(
            {
                "service_id": record.service_id,
                "member_id": record.member_id,
                "token": record.token,
                "refresh_token": record.refresh_token,
                "expires_at": record.expires_at,
                "scopes": list(record.scopes),
                "metadata": record.metadata,
                "added_at": record.added_at,
                "rotated_at": record.rotated_at,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        ciphertext = fernet.encrypt(plaintext)
        # Atomic-ish write: write to temp + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ciphertext)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    def _read(self, path: Path) -> StoredCredential:
        fernet = _fernet_for(self._data_dir, self._instance_id)
        try:
            ciphertext = path.read_bytes()
        except Exception as exc:
            raise RuntimeError(f"Failed to read credential at {path}: {exc}") from exc
        try:
            plaintext = fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise RuntimeError(
                f"Failed to decrypt credential at {path}. The encryption key "
                f"in KERNOS_CREDENTIAL_KEY (or the key file) does not match "
                f"the key used to write this credential. Either restore the "
                f"original key or revoke + re-add the credential."
            ) from exc
        data = json.loads(plaintext)
        return StoredCredential(
            service_id=data["service_id"],
            member_id=data["member_id"],
            token=data["token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=data.get("expires_at"),
            scopes=tuple(data.get("scopes") or ()),
            metadata=dict(data.get("metadata") or {}),
            added_at=int(data.get("added_at", 0)),
            rotated_at=int(data.get("rotated_at", 0)),
        )
