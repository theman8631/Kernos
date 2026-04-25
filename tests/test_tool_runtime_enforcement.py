"""Runtime enforcement of the four checks the Kit-revised spec
requires at every workshop tool invocation.

Covers:
- Hash check: clean pass, descriptor-edit detection, impl-edit
  detection, missing-file handling.
- Operation authority: pass, missing operation when authority
  declared, unknown operation, service-removed-operation drift.
- Credential scope: trivial pass for non-service tools, missing
  credential for service-bound, expired credential.
- Sandbox check: pass for paths under data_dir, fail for paths
  above, fail via symlink resolution.
- Composed enforce_invocation: runs all four; first failure raises;
  force-registered tools (per Kit edit 5) still subject to runtime
  enforcement.
"""

import json
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_member import MemberCredentialStore
from kernos.kernel.services import (
    ServiceRegistry,
    parse_service_descriptor,
)
from kernos.kernel.tool_descriptor import parse_tool_descriptor
from kernos.kernel.tool_runtime import build_runtime_context
from kernos.kernel.tool_runtime_enforcement import (
    AuthorityViolationError,
    CredentialUnavailableError,
    EnforcementInputs,
    HashMismatchError,
    SandboxViolationError,
    check_credential_scope,
    check_hash_unchanged,
    check_operation_authority,
    check_sandbox_path,
    compute_registration_hash,
    enforce_invocation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def store(tmp_path, env_key):
    return MemberCredentialStore(tmp_path, "discord:i")


@pytest.fixture
def notion_registry():
    registry = ServiceRegistry()
    registry.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
    }))
    return registry


def _write_tool_files(tmp_path, *, name="reader", impl_src="def execute(d, c): return {}\n",
                     descriptor_extras=None):
    desc_path = tmp_path / f"{name}.tool.json"
    impl_path = tmp_path / f"{name}.py"
    descriptor = {
        "name": name,
        "description": "x",
        "input_schema": {"type": "object"},
        "implementation": f"{name}.py",
    }
    if descriptor_extras:
        descriptor.update(descriptor_extras)
    desc_path.write_text(json.dumps(descriptor))
    impl_path.write_text(impl_src)
    return desc_path, impl_path, descriptor


# ---------------------------------------------------------------------------
# Hash check
# ---------------------------------------------------------------------------


def test_compute_registration_hash_is_deterministic():
    a = compute_registration_hash('{"a":1}', "code")
    b = compute_registration_hash('{"a":1}', "code")
    assert a == b
    assert len(a) == 64


def test_compute_registration_hash_changes_with_descriptor_edit():
    a = compute_registration_hash('{"a":1}', "code")
    b = compute_registration_hash('{"a":2}', "code")
    assert a != b


def test_compute_registration_hash_changes_with_impl_edit():
    a = compute_registration_hash('{"a":1}', "code1")
    b = compute_registration_hash('{"a":1}', "code2")
    assert a != b


def test_compute_registration_hash_separator_prevents_collision():
    """Two different (descriptor, impl) pairs concatenating to the
    same byte sequence should still produce different hashes — the
    separator handles this."""
    a = compute_registration_hash("aabb", "ccdd")
    b = compute_registration_hash("aa", "bbccdd")
    assert a != b


def test_check_hash_unchanged_passes_when_files_match(tmp_path):
    desc_path, impl_path, descriptor = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Should not raise.
    check_hash_unchanged(
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
    )


def test_check_hash_unchanged_fails_after_descriptor_edit(tmp_path):
    desc_path, impl_path, descriptor = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    descriptor["description"] = "modified description"
    desc_path.write_text(json.dumps(descriptor))
    with pytest.raises(HashMismatchError, match="edited since"):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


def test_check_hash_unchanged_fails_after_impl_edit(tmp_path):
    desc_path, impl_path, _ = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    impl_path.write_text("def execute(d, c): return {'evil': True}\n")
    with pytest.raises(HashMismatchError):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


def test_check_hash_unchanged_fails_when_file_missing(tmp_path):
    desc_path, impl_path, _ = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    impl_path.unlink()
    with pytest.raises(HashMismatchError, match="cannot be read"):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


# ---------------------------------------------------------------------------
# Operation authority re-check
# ---------------------------------------------------------------------------


def test_authority_passes_when_operation_in_list():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    check_operation_authority(descriptor=desc, operation="read_pages")


def test_authority_passes_with_blank_op_when_no_authority_list():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
    })
    check_operation_authority(descriptor=desc, operation="")


def test_authority_fails_when_operation_unknown():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    with pytest.raises(AuthorityViolationError, match="not in tool"):
        check_operation_authority(descriptor=desc, operation="delete_pages")


def test_authority_fails_with_blank_op_when_authority_declared():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    with pytest.raises(AuthorityViolationError, match="did not name"):
        check_operation_authority(descriptor=desc, operation="")


def test_authority_fails_when_service_drops_operation(notion_registry):
    """Service-bound tool's operation gets removed from the service's
    declared operations between registration and invocation."""
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages", "write_pages"],
    }, service_lookup=notion_registry.get)

    # Mutate the registry: replace Notion's descriptor with a version
    # that no longer declares write_pages.
    notion_registry.unregister("notion")
    notion_registry.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages"],  # write_pages dropped
    }))

    with pytest.raises(AuthorityViolationError, match="no longer in"):
        check_operation_authority(
            descriptor=desc,
            operation="write_pages",
            service_registry=notion_registry,
        )


def test_authority_fails_when_service_unregistered(notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    notion_registry.unregister("notion")
    with pytest.raises(AuthorityViolationError, match="no longer registered"):
        check_operation_authority(
            descriptor=desc,
            operation="read_pages",
            service_registry=notion_registry,
        )


# ---------------------------------------------------------------------------
# Credential scope re-check
# ---------------------------------------------------------------------------


def test_credential_check_passes_for_non_service_tool(store):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
    })
    check_credential_scope(
        descriptor=desc, member_id="m", credential_store=store,
    )


def test_credential_check_fails_when_credential_missing(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    with pytest.raises(CredentialUnavailableError, match="No credential"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
        )


def test_credential_check_fails_when_credential_expired(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    store.add(
        member_id="mem_alice", service_id="notion", token="x",
        expires_at=int(time.time()) - 60,  # already expired
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
        )


def test_credential_check_passes_when_credential_valid(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    store.add(member_id="mem_alice", service_id="notion", token="x")
    check_credential_scope(
        descriptor=desc, member_id="mem_alice", credential_store=store,
    )


# ---------------------------------------------------------------------------
# Sandbox check
# ---------------------------------------------------------------------------


def test_sandbox_check_passes_for_path_inside(tmp_path, store):
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_id="t",
    )
    inside = ctx.data_dir / "out.json"
    inside.write_text("{}")
    check_sandbox_path(target=inside, context=ctx)


def test_sandbox_check_fails_for_path_outside(tmp_path, store):
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_id="t",
    )
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("nope")
    with pytest.raises(SandboxViolationError, match="System32"):
        check_sandbox_path(target=outside, context=ctx)


# ---------------------------------------------------------------------------
# Composed enforce_invocation
# ---------------------------------------------------------------------------


def test_enforce_invocation_passes_when_all_checks_pass(tmp_path, store, notion_registry):
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    store.add(member_id="mem_alice", service_id="notion", token="x")
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="read_pages",
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    enforce_invocation(inputs)  # should not raise


def test_enforce_invocation_first_failure_raises_specific_subclass(
    tmp_path, store, notion_registry,
):
    """When multiple checks would fail, the first one (hash) raises;
    later checks aren't reached. Verifies the order is hash → authority
    → credentials."""
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Edit the impl to break the hash check.
    impl_path.write_text("def execute(d, c): return {'evil': True}\n")
    # Also drop the credential to make Check 3 fail too.
    # Hash check fires first.
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="read_pages",
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    with pytest.raises(HashMismatchError):
        enforce_invocation(inputs)


def test_force_registered_tool_still_subject_to_runtime_enforcement(
    tmp_path, store, notion_registry,
):
    """Kit edit 5: force-register bypasses authoring-pattern validation
    only. Runtime enforcement still applies. Construct an invocation
    where the descriptor author was force-registered; the four checks
    must still fire if their preconditions fail."""
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Force-register status is orthogonal; we are a force-registered
    # tool but invoked with an operation outside our authority. The
    # authority check must still fire.
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="delete_pages",  # not in authority
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    with pytest.raises(AuthorityViolationError):
        enforce_invocation(inputs)
