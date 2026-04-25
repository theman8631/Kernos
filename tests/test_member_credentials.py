"""Tests for the per-member credentials store.

Covers: round-trip, rotation, revocation, member isolation, key
resolution (env var vs file vs auto-generation), expiry semantics,
encryption-at-rest sanity, and corruption / wrong-key error paths.
"""

import json
import os
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
    StoredCredential,
    _credential_path,
    _key_path,
    _resolve_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Fresh store with a known key in the env var (no first-run notice)."""
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    return MemberCredentialStore(tmp_path, "discord:owner-id-1")


@pytest.fixture
def autokey_store(tmp_path, monkeypatch):
    """Store that has to auto-generate its key (no env var)."""
    monkeypatch.delenv("KERNOS_CREDENTIAL_KEY", raising=False)
    return MemberCredentialStore(tmp_path, "discord:owner-id-1")


# ---------------------------------------------------------------------------
# Round-trip + lifecycle
# ---------------------------------------------------------------------------


def test_add_and_get_round_trip(store):
    record = store.add(
        member_id="mem_alice",
        service_id="notion",
        token="secret_abc",
        scopes=("read_pages", "write_pages"),
    )
    fetched = store.get(member_id="mem_alice", service_id="notion")
    assert fetched.token == "secret_abc"
    assert fetched.scopes == ("read_pages", "write_pages")
    assert fetched.added_at > 0
    assert fetched.rotated_at == fetched.added_at


def test_has_returns_true_only_when_credential_exists(store):
    assert store.has(member_id="mem_alice", service_id="notion") is False
    store.add(member_id="mem_alice", service_id="notion", token="x")
    assert store.has(member_id="mem_alice", service_id="notion") is True


def test_get_raises_when_missing(store):
    with pytest.raises(MemberCredentialNotFound):
        store.get(member_id="mem_alice", service_id="notion")


def test_rotate_updates_token_and_rotated_at(store):
    record = store.add(member_id="mem_alice", service_id="notion", token="old")
    initial_added = record.added_at
    initial_rotated = record.rotated_at
    time.sleep(0.01)  # ensure rotated_at can move forward

    rotated = store.rotate(
        member_id="mem_alice", service_id="notion", token="new", expires_at=999999999,
    )
    assert rotated.token == "new"
    assert rotated.expires_at == 999999999
    assert rotated.added_at == initial_added  # preserved
    assert rotated.rotated_at >= initial_rotated  # non-decreasing


def test_rotate_preserves_metadata_and_scopes(store):
    store.add(
        member_id="mem_alice",
        service_id="notion",
        token="t1",
        scopes=("read_pages",),
        metadata={"workspace_id": "ws-42"},
    )
    rotated = store.rotate(member_id="mem_alice", service_id="notion", token="t2")
    assert rotated.scopes == ("read_pages",)
    assert rotated.metadata == {"workspace_id": "ws-42"}


def test_revoke_returns_true_then_false(store):
    store.add(member_id="mem_alice", service_id="notion", token="x")
    assert store.revoke(member_id="mem_alice", service_id="notion") is True
    assert store.has(member_id="mem_alice", service_id="notion") is False
    # Second revoke is a no-op that returns False rather than crashing.
    assert store.revoke(member_id="mem_alice", service_id="notion") is False


def test_add_overwrites_existing_credential(store):
    store.add(member_id="mem_alice", service_id="notion", token="first")
    store.add(member_id="mem_alice", service_id="notion", token="second")
    assert store.get(member_id="mem_alice", service_id="notion").token == "second"


# ---------------------------------------------------------------------------
# Member isolation
# ---------------------------------------------------------------------------


def test_credentials_are_isolated_per_member(store):
    store.add(member_id="mem_alice", service_id="notion", token="alice-token")
    store.add(member_id="mem_bob", service_id="notion", token="bob-token")
    assert store.get(member_id="mem_alice", service_id="notion").token == "alice-token"
    assert store.get(member_id="mem_bob", service_id="notion").token == "bob-token"


def test_revoke_does_not_touch_other_members(store):
    store.add(member_id="mem_alice", service_id="notion", token="a")
    store.add(member_id="mem_bob", service_id="notion", token="b")
    store.revoke(member_id="mem_alice", service_id="notion")
    assert store.has(member_id="mem_bob", service_id="notion") is True


def test_list_services_only_returns_invoking_member(store):
    store.add(member_id="mem_alice", service_id="notion", token="x")
    store.add(member_id="mem_alice", service_id="github", token="y")
    store.add(member_id="mem_bob", service_id="notion", token="z")
    services = store.list_services_for_member("mem_alice")
    assert sorted(services) == ["github", "notion"]
    assert store.list_services_for_member("mem_bob") == ["notion"]


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------


def test_stored_file_does_not_contain_plaintext_token(store, tmp_path):
    store.add(
        member_id="mem_alice",
        service_id="notion",
        token="THIS_TOKEN_MUST_NOT_LEAK",
    )
    path = _credential_path(tmp_path, "discord:owner-id-1", "mem_alice", "notion")
    raw = path.read_bytes()
    assert b"THIS_TOKEN_MUST_NOT_LEAK" not in raw
    # Fernet ciphertexts begin with the version byte (0x80) base64-encoded.
    assert raw.startswith(b"gAAAAAB")


def test_wrong_key_fails_to_decrypt(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store_a = MemberCredentialStore(tmp_path, "discord:i")
    store_a.add(member_id="mem_alice", service_id="notion", token="x")

    # New store with a different key sees the same file but cannot decrypt.
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store_b = MemberCredentialStore(tmp_path, "discord:i")
    with pytest.raises(RuntimeError, match="does not match"):
        store_b.get(member_id="mem_alice", service_id="notion")


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def test_env_var_key_takes_precedence_over_file(tmp_path, monkeypatch):
    env_key = Fernet.generate_key()
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", env_key.decode())
    # Create a file with a *different* key. The env var wins.
    key_path = _key_path(tmp_path, "i")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(Fernet.generate_key())
    resolved = _resolve_key(tmp_path, "i")
    assert resolved == env_key


def test_auto_generates_key_when_no_env_var(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("KERNOS_CREDENTIAL_KEY", raising=False)
    import logging
    caplog.set_level(logging.WARNING)
    key1 = _resolve_key(tmp_path, "i")
    # File is created with the auto-generated key.
    assert _key_path(tmp_path, "i").exists()
    # First-run back-up notice is logged.
    assert any("CREDENTIAL_KEY_GENERATED" in r.getMessage() for r in caplog.records)
    # Second resolution returns the same key (no regeneration).
    key2 = _resolve_key(tmp_path, "i")
    assert key1 == key2


def test_auto_generated_key_file_has_0600_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_CREDENTIAL_KEY", raising=False)
    _resolve_key(tmp_path, "i")
    path = _key_path(tmp_path, "i")
    mode = oct(os.stat(path).st_mode & 0o777)
    assert mode == "0o600"


def test_invalid_env_key_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", "not-a-valid-fernet-key")
    store = MemberCredentialStore(tmp_path, "i")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        store.add(member_id="m", service_id="s", token="t")


# ---------------------------------------------------------------------------
# Expiry semantics
# ---------------------------------------------------------------------------


def test_is_expired_returns_false_for_no_expiry(store):
    store.add(member_id="m", service_id="s", token="t")
    assert store.get(member_id="m", service_id="s").is_expired is False


def test_is_expired_returns_true_for_past_expires_at(store):
    store.add(member_id="m", service_id="s", token="t", expires_at=1)
    assert store.get(member_id="m", service_id="s").is_expired is True


def test_is_expired_returns_false_for_future_expires_at(store):
    store.add(
        member_id="m", service_id="s", token="t",
        expires_at=int(time.time()) + 60,
    )
    assert store.get(member_id="m", service_id="s").is_expired is False


# ---------------------------------------------------------------------------
# Auto-generation flow on first add (end-to-end smoke)
# ---------------------------------------------------------------------------


def test_first_add_auto_generates_key_and_round_trips(autokey_store, tmp_path):
    autokey_store.add(member_id="mem_alice", service_id="notion", token="t")
    fetched = autokey_store.get(member_id="mem_alice", service_id="notion")
    assert fetched.token == "t"
    # The auto-generated key is on disk with 0600.
    assert _key_path(tmp_path, "discord:owner-id-1").exists()
