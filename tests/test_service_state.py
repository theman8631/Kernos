"""Tests for the install-level ServiceState + ServiceStateStore.

Covers Section 1, 1a, 10 of the INSTALL-FOR-STOCK-CONNECTORS spec:
schema validation + provenance fields, atomic write semantics,
cache invalidation, migration helper for pre-spec installs.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kernos.setup.service_state import (
    ServiceState,
    ServiceStateError,
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
    install_appears_existing,
    migrate_if_needed,
    synthesize_migration_state,
)


# ---------------------------------------------------------------------------
# ServiceState
# ---------------------------------------------------------------------------


def _state(
    service_id: str = "notion",
    *,
    enabled: bool = True,
    source: ServiceStateSource = ServiceStateSource.SETUP,
    updated_by: ServiceStateUpdatedBy = ServiceStateUpdatedBy.OPERATOR,
    updated_at: str = "2026-04-26T00:00:00+00:00",
    reason: str = "",
) -> ServiceState:
    return ServiceState(
        service_id=service_id,
        enabled=enabled,
        source=source,
        updated_at=updated_at,
        updated_by=updated_by,
        reason=reason,
    )


def test_service_state_round_trip():
    s = _state(reason="enabled at first-run setup")
    payload = s.to_dict()
    assert payload == {
        "service_id": "notion",
        "enabled": True,
        "source": "setup",
        "updated_at": "2026-04-26T00:00:00+00:00",
        "updated_by": "operator",
        "reason": "enabled at first-run setup",
    }
    assert ServiceState.from_dict(payload) == s


def test_service_state_rejects_empty_service_id():
    with pytest.raises(ServiceStateError, match="service_id"):
        _state(service_id="")


def test_service_state_rejects_non_bool_enabled():
    with pytest.raises(ServiceStateError, match="enabled"):
        ServiceState(
            service_id="x",
            enabled="yes",  # type: ignore[arg-type]
            source=ServiceStateSource.SETUP,
            updated_at="t",
            updated_by=ServiceStateUpdatedBy.OPERATOR,
        )


def test_service_state_rejects_bad_source():
    with pytest.raises(ServiceStateError, match="source"):
        ServiceState.from_dict(
            {
                "service_id": "x",
                "enabled": True,
                "source": "ghost",
                "updated_at": "t",
                "updated_by": "operator",
                "reason": "",
            }
        )


def test_service_state_rejects_bad_updated_by():
    with pytest.raises(ServiceStateError, match="updated_by"):
        ServiceState.from_dict(
            {
                "service_id": "x",
                "enabled": True,
                "source": "setup",
                "updated_at": "t",
                "updated_by": "alien",
                "reason": "",
            }
        )


# ---------------------------------------------------------------------------
# ServiceStateStore — basic reads
# ---------------------------------------------------------------------------


def test_store_path_is_under_install_subdir(tmp_path):
    store = ServiceStateStore(tmp_path)
    assert store.path == tmp_path / "install" / "service_state.json"
    assert store.exists() is False


def test_store_returns_empty_when_no_file(tmp_path):
    store = ServiceStateStore(tmp_path)
    assert store.list_all() == ()
    assert store.is_enabled("anything") is False
    assert store.disabled_service_ids() == set()


def test_store_is_enabled_default_false_for_unknown_service(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    assert store.is_enabled("notion") is True
    # Unknown service is treated as disabled — conservative default.
    assert store.is_enabled("never_registered") is False


# ---------------------------------------------------------------------------
# ServiceStateStore — writes (atomic + invalidating)
# ---------------------------------------------------------------------------


def test_store_set_creates_file_and_round_trips(tmp_path):
    store = ServiceStateStore(tmp_path)
    state = store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
        reason="picked at setup",
    )
    assert store.exists()
    assert state.enabled is True
    # Reading from disk via a fresh store reflects the same state.
    fresh = ServiceStateStore(tmp_path)
    got = fresh.get("notion")
    assert got is not None
    assert got.enabled is True
    assert got.reason == "picked at setup"
    assert got.source is ServiceStateSource.SETUP


def test_store_set_updates_provenance_each_call(tmp_path):
    store = ServiceStateStore(tmp_path)
    a = store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    b = store.set(
        "notion",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
        reason="disabling for now",
    )
    assert a.enabled is True
    assert b.enabled is False
    assert b.source is ServiceStateSource.OPERATOR
    assert b.reason == "disabling for now"


def test_store_set_bulk_writes_one_atomic_snapshot(tmp_path):
    store = ServiceStateStore(tmp_path)
    states = (
        _state("notion", enabled=True),
        _state("drive", enabled=False),
        _state("github", enabled=True),
    )
    written = store.set_bulk(states)
    assert len(written) == 3
    listed = store.list_all()
    assert [s.service_id for s in listed] == ["drive", "github", "notion"]


def test_store_replace_all_drops_unlisted_entries(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    store.set(
        "drive",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    store.replace_all([_state("github", enabled=True)])
    listed = store.list_all()
    assert [s.service_id for s in listed] == ["github"]


def test_store_atomic_write_via_temp_file(tmp_path):
    """Writes go through a sibling temp file and atomic rename."""
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    install_dir = tmp_path / "install"
    # No leftover .tmp sibling after a successful write.
    leftovers = [p for p in install_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_store_invalidate_drops_cache_and_reloads(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    # External mutation: another writer flips notion to disabled.
    other = ServiceStateStore(tmp_path)
    other.set(
        "notion",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    # The first store's cache still says enabled.
    assert store.get("notion").enabled is True
    store.invalidate()
    assert store.get("notion").enabled is False


# ---------------------------------------------------------------------------
# ServiceStateStore — schema + corruption handling
# ---------------------------------------------------------------------------


def test_store_persists_schema_version(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    raw = json.loads(store.path.read_text())
    assert raw["schema_version"] == 1
    assert isinstance(raw["services"], list)


def test_store_rejects_corrupt_json(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "service_state.json").write_text("not json")
    store = ServiceStateStore(tmp_path)
    with pytest.raises(ServiceStateError, match="failed to read"):
        store.list_all()


def test_store_rejects_duplicate_service_id_in_file(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "service_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": [
                    _state("notion").to_dict(),
                    _state("notion").to_dict(),
                ],
            }
        )
    )
    store = ServiceStateStore(tmp_path)
    with pytest.raises(ServiceStateError, match="duplicate"):
        store.list_all()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_store_concurrent_writes_do_not_corrupt(tmp_path):
    store = ServiceStateStore(tmp_path)
    errors = []

    def writer(sid: str):
        try:
            for i in range(10):
                store.set(
                    sid,
                    enabled=(i % 2 == 0),
                    source=ServiceStateSource.OPERATOR,
                    updated_by=ServiceStateUpdatedBy.OPERATOR,
                    reason=f"iter-{i}",
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(f"svc_{n}",)) for n in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    states = store.list_all()
    assert len(states) == 5
    # File still parses cleanly.
    raw = json.loads(store.path.read_text())
    assert raw["schema_version"] == 1


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_synthesize_migration_state_marks_each_service_enabled():
    states = synthesize_migration_state(["notion", "drive", "github"])
    assert len(states) == 3
    assert all(s.enabled for s in states)
    assert all(s.source is ServiceStateSource.MIGRATION for s in states)
    assert all(
        s.updated_by is ServiceStateUpdatedBy.MIGRATION for s in states
    )
    # Sorted alphabetically + de-duplicated.
    assert [s.service_id for s in states] == ["drive", "github", "notion"]


def test_synthesize_migration_state_dedupes():
    states = synthesize_migration_state(["notion", "notion", "drive"])
    assert [s.service_id for s in states] == ["drive", "notion"]


def test_install_appears_existing_false_for_missing_dir(tmp_path):
    assert install_appears_existing(tmp_path / "nonexistent") is False


def test_install_appears_existing_false_for_empty_dir(tmp_path):
    assert install_appears_existing(tmp_path) is False


def test_install_appears_existing_false_when_only_install_subdir(tmp_path):
    (tmp_path / "install").mkdir()
    assert install_appears_existing(tmp_path) is False


def test_install_appears_existing_true_when_instance_dir_present(tmp_path):
    (tmp_path / "discord_12345").mkdir()
    assert install_appears_existing(tmp_path) is True


def test_install_appears_existing_true_with_install_plus_instance(tmp_path):
    (tmp_path / "install").mkdir()
    (tmp_path / "telegram_42").mkdir()
    assert install_appears_existing(tmp_path) is True


def test_migrate_if_needed_writes_when_state_absent_and_install_existing(tmp_path):
    (tmp_path / "discord_12345").mkdir()
    store = ServiceStateStore(tmp_path)
    written = migrate_if_needed(
        store,
        ["notion", "drive"],
        install_appears_existing=True,
    )
    assert len(written) == 2
    assert store.exists()
    assert all(s.source is ServiceStateSource.MIGRATION for s in written)
    assert store.is_enabled("notion") is True
    assert store.is_enabled("drive") is True


def test_migrate_if_needed_skips_when_state_already_exists(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "notion",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    written = migrate_if_needed(
        store,
        ["notion", "drive"],
        install_appears_existing=True,
    )
    assert written == ()
    # Existing state untouched — notion stays disabled, drive stays unknown.
    assert store.is_enabled("notion") is False
    assert store.get("drive") is None


def test_migrate_if_needed_skips_when_install_looks_fresh(tmp_path):
    store = ServiceStateStore(tmp_path)
    written = migrate_if_needed(
        store,
        ["notion", "drive"],
        install_appears_existing=False,
    )
    assert written == ()
    assert not store.exists()


# ---------------------------------------------------------------------------
# disabled_service_ids surface
# ---------------------------------------------------------------------------


def test_disabled_service_ids_returns_explicit_disabled_only(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set_bulk(
        [
            _state("notion", enabled=True),
            _state("drive", enabled=False),
            _state("github", enabled=False),
        ]
    )
    assert store.disabled_service_ids() == {"drive", "github"}
