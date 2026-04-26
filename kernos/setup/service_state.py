"""Per-install service enable/disable state.

Per the INSTALL-FOR-STOCK-CONNECTORS spec (Section 1, 1a):

  - `ServiceState` dataclass: per-service enable flag plus full
    provenance (source / updated_at / updated_by / reason).
  - `ServiceStateStore` class: single source of truth. All reads
    go through the store; all writes are atomic (write-temp +
    rename) so concurrent callers never see a torn JSON file.
    Cache invalidation triggers on every write so the surfacing
    layer and the dispatch layer pick up changes immediately.
  - Migration helper: existing installs (data dir present, no
    service_state.json) get a synthetic all-enabled state with
    `source: "migration"`. Headless-safe — no prompts, no blocks
    (Section 10).

State persists at `<data_dir>/install/service_state.json`. The
install path is cross-instance: a single Kernos install has one
service-state file regardless of how many instances live under
the data directory.

The store does NOT generate the credential key, validate
permissions, or invoke install hooks. Those belong to other
specs (credential key surfacing in Section 6; hook runner in
Section 7). The store's contract is purely state read/write.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from kernos.utils import utc_now


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ServiceStateError(ValueError):
    """Raised when ServiceState validation or store operations fail."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ServiceStateSource(str, Enum):
    """Provenance of a ServiceState's current value.

    Per spec Section 1: source captures where the current state
    came from. Setup-time choices land as `setup`; later operator
    edits via `kernos services enable/disable` land as `operator`;
    the migration path for pre-spec installs lands as `migration`;
    the `default` value is reserved for descriptor-supplied
    defaults when neither setup nor migration has run yet.
    """

    DEFAULT = "default"
    OPERATOR = "operator"
    SETUP = "setup"
    MIGRATION = "migration"


class ServiceStateUpdatedBy(str, Enum):
    """Who or what set the current value.

    Distinct from `source` because an operator running through
    setup writes `source=setup`, `updated_by=operator`, while a
    headless migration writes `source=migration`,
    `updated_by=migration`.
    """

    OPERATOR = "operator"
    SYSTEM = "system"
    MIGRATION = "migration"


# ---------------------------------------------------------------------------
# ServiceState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceState:
    """Per-service install state.

    `service_id` matches the service descriptor's id (registered
    via ServiceRegistry). `enabled` is the operator's choice —
    when False, the service's tools are filtered from agent
    surfaces and dispatch refuses invocation (per the two-layer
    enforcement in Section 2). The remaining fields carry full
    provenance for the audit surface and `kernos services info`.
    """

    service_id: str
    enabled: bool
    source: ServiceStateSource
    updated_at: str  # ISO 8601 UTC
    updated_by: ServiceStateUpdatedBy
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.service_id, str) or not self.service_id.strip():
            raise ServiceStateError(
                "ServiceState.service_id must be a non-empty string"
            )
        if not isinstance(self.enabled, bool):
            raise ServiceStateError("ServiceState.enabled must be a bool")
        if not isinstance(self.source, ServiceStateSource):
            raise ServiceStateError(
                f"ServiceState.source must be a ServiceStateSource; got "
                f"{type(self.source).__name__}"
            )
        if not isinstance(self.updated_by, ServiceStateUpdatedBy):
            raise ServiceStateError(
                f"ServiceState.updated_by must be a ServiceStateUpdatedBy; "
                f"got {type(self.updated_by).__name__}"
            )
        if not isinstance(self.updated_at, str) or not self.updated_at.strip():
            raise ServiceStateError(
                "ServiceState.updated_at must be a non-empty ISO 8601 string"
            )
        if not isinstance(self.reason, str):
            raise ServiceStateError("ServiceState.reason must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "enabled": self.enabled,
            "source": self.source.value,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceState":
        if not isinstance(data, dict):
            raise ServiceStateError(
                f"ServiceState must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        try:
            source = ServiceStateSource(data.get("source", ""))
        except ValueError as exc:
            valid = ", ".join(s.value for s in ServiceStateSource)
            raise ServiceStateError(
                f"ServiceState.source {data.get('source')!r} is not one of: "
                f"{valid}"
            ) from exc
        try:
            updated_by = ServiceStateUpdatedBy(data.get("updated_by", ""))
        except ValueError as exc:
            valid = ", ".join(u.value for u in ServiceStateUpdatedBy)
            raise ServiceStateError(
                f"ServiceState.updated_by {data.get('updated_by')!r} is not "
                f"one of: {valid}"
            ) from exc
        return cls(
            service_id=str(data.get("service_id", "")),
            enabled=bool(data.get("enabled", False)),
            source=source,
            updated_at=str(data.get("updated_at", "")),
            updated_by=updated_by,
            reason=str(data.get("reason", "")),
        )


# ---------------------------------------------------------------------------
# ServiceStateStore
# ---------------------------------------------------------------------------


_STATE_RELATIVE_PATH = ("install", "service_state.json")
_SCHEMA_VERSION = 1


class ServiceStateStore:
    """Single source of truth for per-install service enable state.

    Construction takes a `data_dir`; the store places its file at
    `<data_dir>/install/service_state.json` (path constants live
    here so callers don't recompute). Reads cache aggressively;
    writes invalidate. Concurrent reads are safe behind a reentrant
    lock; writes are atomic via temp-file plus rename.

    The store does NOT bind a list of registered service ids — it
    holds whatever state the CLI / setup flow / migration helper
    persisted. Callers that need to reconcile against the active
    registry (e.g., `kernos services list`) read the registry
    separately and join in the caller layer.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._path = self._data_dir.joinpath(*_STATE_RELATIVE_PATH)
        self._cache: dict[str, ServiceState] | None = None
        self._lock = threading.RLock()

    # ----- path helpers -----

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def exists(self) -> bool:
        return self._path.exists()

    # ----- reads -----

    def _ensure_loaded(self) -> dict[str, ServiceState]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not self._path.exists():
                self._cache = {}
                return self._cache
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ServiceStateError(
                    f"failed to read service_state at {self._path}: {exc}"
                ) from exc
            self._cache = _parse_state_document(raw)
            return self._cache

    def get(self, service_id: str) -> ServiceState | None:
        return self._ensure_loaded().get(service_id)

    def is_enabled(self, service_id: str) -> bool:
        """Return True only if the service is explicitly enabled.

        Conservative default: a service with no recorded state is
        treated as disabled. This is the spec's stance — fresh
        installs auto-bootstrap all-disabled in non-TTY contexts;
        operators must run setup or `kernos services enable` to
        flip a service on.
        """
        state = self.get(service_id)
        return bool(state and state.enabled)

    def list_all(self) -> tuple[ServiceState, ...]:
        """Return all known states in alphabetical service_id order."""
        states = self._ensure_loaded().values()
        return tuple(sorted(states, key=lambda s: s.service_id))

    def disabled_service_ids(self) -> set[str]:
        """Set of service_ids whose stored state is disabled.

        Services with no stored state are NOT in this set — callers
        treating them as disabled (per `is_enabled` semantics) need
        to derive that separately. This method exists for code paths
        that want explicit disabled-only filtering.
        """
        return {
            s.service_id
            for s in self._ensure_loaded().values()
            if not s.enabled
        }

    # ----- writes (atomic, invalidating) -----

    def set(
        self,
        service_id: str,
        *,
        enabled: bool,
        source: ServiceStateSource,
        updated_by: ServiceStateUpdatedBy,
        reason: str = "",
    ) -> ServiceState:
        """Set a single service's state. Atomic; invalidates cache."""
        new_state = ServiceState(
            service_id=service_id,
            enabled=enabled,
            source=source,
            updated_at=utc_now(),
            updated_by=updated_by,
            reason=reason,
        )
        with self._lock:
            current = dict(self._ensure_loaded())
            current[service_id] = new_state
            self._write_atomic(current)
            self._cache = current
        return new_state

    def set_bulk(
        self,
        states: Iterable[ServiceState],
    ) -> tuple[ServiceState, ...]:
        """Replace multiple states in a single atomic write.

        Used by setup completion and migration: both want one
        coherent snapshot rather than N individual writes.
        """
        with self._lock:
            current = dict(self._ensure_loaded())
            written: list[ServiceState] = []
            for s in states:
                if not isinstance(s, ServiceState):
                    raise ServiceStateError(
                        f"set_bulk expected ServiceState; got "
                        f"{type(s).__name__}"
                    )
                current[s.service_id] = s
                written.append(s)
            self._write_atomic(current)
            self._cache = current
        return tuple(written)

    def replace_all(self, states: Iterable[ServiceState]) -> tuple[ServiceState, ...]:
        """Overwrite the entire state file with the given states.

        Used by migration to seed a fresh state file. Existing
        entries not in `states` are dropped.
        """
        with self._lock:
            mapping: dict[str, ServiceState] = {}
            written: list[ServiceState] = []
            for s in states:
                if not isinstance(s, ServiceState):
                    raise ServiceStateError(
                        f"replace_all expected ServiceState; got "
                        f"{type(s).__name__}"
                    )
                mapping[s.service_id] = s
                written.append(s)
            self._write_atomic(mapping)
            self._cache = mapping
        return tuple(written)

    def invalidate(self) -> None:
        """Drop the in-process cache.

        Useful for long-running daemons that want to pick up
        external CLI mutations on the next read without restarting.
        """
        with self._lock:
            self._cache = None

    # ----- internals -----

    def _write_atomic(self, mapping: dict[str, ServiceState]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": _SCHEMA_VERSION,
            "services": [s.to_dict() for s in mapping.values()],
        }
        # Write to a temp sibling, then rename — POSIX atomic.
        # On Windows, os.replace is also atomic per Python docs.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(document, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)
        except OSError as exc:
            # Best-effort cleanup of the temp file on failure.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover
                pass
            raise ServiceStateError(
                f"failed to write service_state at {self._path}: {exc}"
            ) from exc


def _parse_state_document(raw: Any) -> dict[str, ServiceState]:
    if not isinstance(raw, dict):
        raise ServiceStateError(
            f"service_state.json must contain a JSON object; got "
            f"{type(raw).__name__}"
        )
    services_raw = raw.get("services", [])
    if not isinstance(services_raw, list):
        raise ServiceStateError(
            "service_state.json 'services' field must be a list"
        )
    parsed: dict[str, ServiceState] = {}
    for entry in services_raw:
        s = ServiceState.from_dict(entry)
        if s.service_id in parsed:
            raise ServiceStateError(
                f"service_state.json contains duplicate entry for "
                f"{s.service_id!r}"
            )
        parsed[s.service_id] = s
    return parsed


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def synthesize_migration_state(
    service_ids: Iterable[str],
    *,
    now: str | None = None,
) -> tuple[ServiceState, ...]:
    """Build all-enabled ServiceStates for an existing install.

    Per spec Section 10: pre-spec installs preserve current
    behavior (everything visible). Source: "migration",
    updated_by: "migration". Headless-safe — produces no I/O,
    just the records.
    """
    timestamp = now or utc_now()
    return tuple(
        ServiceState(
            service_id=sid,
            enabled=True,
            source=ServiceStateSource.MIGRATION,
            updated_at=timestamp,
            updated_by=ServiceStateUpdatedBy.MIGRATION,
            reason="auto-migrated; pre-spec install preserved enabled by default",
        )
        for sid in sorted(set(service_ids))
    )


def migrate_if_needed(
    store: ServiceStateStore,
    service_ids: Iterable[str],
    *,
    install_appears_existing: bool,
) -> tuple[ServiceState, ...]:
    """Auto-migrate when the store has no file and the install is
    pre-existing.

    Returns the written states (empty tuple if no migration ran).
    Caller is responsible for the "is this an existing install?"
    detection and for surfacing the post-migration TTY review
    prompt — this helper is the data-side primitive only.
    """
    if store.exists() or not install_appears_existing:
        return ()
    states = synthesize_migration_state(service_ids)
    return store.replace_all(states)


def install_appears_existing(data_dir: str | Path) -> bool:
    """Heuristic: does this `data_dir` look like a pre-spec install?

    Yes iff the data dir exists and contains at least one
    subdirectory other than `install/`. Instance directories
    (`discord_*`, `telegram_*`, etc.) are the canonical signal —
    a real Kernos install always has at least one instance dir.
    Fresh clones with no data directory return False, so the
    first-run flow can trigger interactive setup instead of
    silent migration.
    """
    path = Path(data_dir).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_dir() and child.name != "install":
            return True
    return False


__all__ = [
    "ServiceState",
    "ServiceStateError",
    "ServiceStateSource",
    "ServiceStateStore",
    "ServiceStateUpdatedBy",
    "install_appears_existing",
    "migrate_if_needed",
    "synthesize_migration_state",
]
