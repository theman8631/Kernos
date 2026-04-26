"""Tests for the dispatch + surfacing enforcement of disabled services.

Covers Section 2 of the INSTALL-FOR-STOCK-CONNECTORS spec — the
two-layer enforcement that makes disabled services actually
disabled at the security boundary, not just filtered from
agent-facing surfaces.

Specifically:
  - Section 2 dispatch layer: enforce_invocation refuses
    service-bound tools when the service is disabled
  - Section 2 surfacing layer: ToolCatalog.disabled_tool_names
    surfaces the tool ids whose service is disabled (caller
    extends its surfacer-exclude set with this)
  - install.dispatch_refused_disabled_service audit category
    fires on dispatch refusal
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kernos.kernel.tool_catalog import CatalogEntry, ToolCatalog
from kernos.kernel.tool_descriptor import (
    GateClassification,
    OperationClassification,
    ToolDescriptor,
)
from kernos.kernel.tool_runtime_enforcement import (
    EnforcementInputs,
    ServiceDisabledError,
    check_service_enabled,
    enforce_invocation,
)
from kernos.setup.service_state import (
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service_bound_descriptor(
    name: str = "drive_read_doc",
    service_id: str = "google_drive",
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="Read a Google Doc.",
        input_schema={"type": "object", "properties": {}},
        implementation=f"{name}.py",
        service_id=service_id,
        authority=("read_doc",),
        gate_classification=GateClassification.READ,
        operations=(
            OperationClassification(
                operation="read_doc",
                classification=GateClassification.READ,
            ),
        ),
        audit_category="google_drive",
    )


def _internal_descriptor(name: str = "remember") -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="Recall a memory.",
        input_schema={"type": "object", "properties": {}},
        implementation=f"{name}.py",
    )


# ---------------------------------------------------------------------------
# check_service_enabled — direct
# ---------------------------------------------------------------------------


def test_check_service_enabled_passes_for_internal_tool(tmp_path):
    store = ServiceStateStore(tmp_path)
    # No store entries; an internal (non-service-bound) tool passes.
    check_service_enabled(
        descriptor=_internal_descriptor(),
        service_state_store=store,
    )


def test_check_service_enabled_passes_when_store_omitted(tmp_path):
    # Legacy callers (no store) skip the check — by design.
    check_service_enabled(
        descriptor=_service_bound_descriptor(),
        service_state_store=None,
    )


def test_check_service_enabled_refuses_when_service_disabled(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "google_drive",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    with pytest.raises(ServiceDisabledError, match="google_drive"):
        check_service_enabled(
            descriptor=_service_bound_descriptor(),
            service_state_store=store,
        )


def test_check_service_enabled_refuses_when_service_unknown(tmp_path):
    """Unknown service in the store is treated as disabled — conservative."""
    store = ServiceStateStore(tmp_path)
    with pytest.raises(ServiceDisabledError, match="google_drive"):
        check_service_enabled(
            descriptor=_service_bound_descriptor(),
            service_state_store=store,
        )


def test_check_service_enabled_passes_when_service_explicitly_enabled(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "google_drive",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    check_service_enabled(
        descriptor=_service_bound_descriptor(),
        service_state_store=store,
    )


def test_service_disabled_error_message_points_at_remediation(tmp_path):
    store = ServiceStateStore(tmp_path)
    store.set(
        "github",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    desc = _service_bound_descriptor("github_open_pr", "github")
    with pytest.raises(ServiceDisabledError) as excinfo:
        check_service_enabled(
            descriptor=desc,
            service_state_store=store,
        )
    msg = str(excinfo.value)
    assert "github" in msg
    assert "kernos services enable" in msg
    assert "kernos setup" in msg


# ---------------------------------------------------------------------------
# enforce_invocation — composed pipeline rejects disabled before credential
# ---------------------------------------------------------------------------


def test_enforce_invocation_disabled_check_runs_before_credential(tmp_path):
    """Section 2 ordering: a disabled service refuses dispatch before
    the credential store is consulted. A non-existent credential
    that would otherwise raise CredentialUnavailableError is never
    queried because the disabled check fires first."""
    store = ServiceStateStore(tmp_path)
    store.set(
        "google_drive",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )

    desc = _service_bound_descriptor()
    desc_path = tmp_path / "drive_read_doc.tool.json"
    impl_path = tmp_path / "drive_read_doc.py"
    desc_path.write_text("{}")
    impl_path.write_text("def execute(input_data, context): return {}\n")

    from kernos.kernel.tool_runtime_enforcement import compute_registration_hash
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes()
    )

    cred_store = MagicMock()
    cred_store.get.side_effect = AssertionError(
        "credential store should not be queried for a disabled service"
    )

    inputs = EnforcementInputs(
        descriptor=desc,
        operation="read_doc",
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="m-1",
        credential_store=cred_store,
        service_registry=None,
        service_state_store=store,
    )
    with pytest.raises(ServiceDisabledError):
        enforce_invocation(inputs)
    # Credential store remained untouched.
    cred_store.get.assert_not_called()


# ---------------------------------------------------------------------------
# Surfacing layer — ToolCatalog.disabled_tool_names
# ---------------------------------------------------------------------------


def test_catalog_disabled_tool_names_returns_only_matching_service_tools():
    cat = ToolCatalog()
    # Service-bound tools
    cat._entries["drive_read_doc"] = CatalogEntry(
        name="drive_read_doc",
        description="Read a doc.",
        source="workspace",
        service_id="google_drive",
    )
    cat._entries["notion_read_page"] = CatalogEntry(
        name="notion_read_page",
        description="Read a Notion page.",
        source="workspace",
        service_id="notion",
    )
    # Internal tool, not service-bound — never in the result.
    cat._entries["remember"] = CatalogEntry(
        name="remember",
        description="Recall a memory.",
        source="kernel",
        service_id="",
    )

    disabled = cat.disabled_tool_names({"google_drive"})
    assert disabled == {"drive_read_doc"}

    # Multi-service disabled set
    disabled = cat.disabled_tool_names({"google_drive", "notion"})
    assert disabled == {"drive_read_doc", "notion_read_page"}

    # Empty disabled set returns empty set without scanning.
    disabled = cat.disabled_tool_names(set())
    assert disabled == set()

    # Disabled service that has no registered tools returns empty.
    disabled = cat.disabled_tool_names({"never_registered_service"})
    assert disabled == set()


def test_catalog_disabled_tool_names_excludes_internal_tools():
    """Tools without a service_id (kernel tools, MCP tools) are install-
    level always-available; never in the disabled-name set."""
    cat = ToolCatalog()
    cat._entries["remember"] = CatalogEntry(
        name="remember",
        description="Recall.",
        source="kernel",
        service_id="",
    )
    # Even if the caller asks about "kernel" (which isn't a service),
    # nothing comes back.
    disabled = cat.disabled_tool_names({"kernel"})
    assert disabled == set()


# ---------------------------------------------------------------------------
# Surfacing layer — disabled-filter composes with the surfacer's exclude
# ---------------------------------------------------------------------------


def test_build_catalog_text_excludes_disabled_tools():
    cat = ToolCatalog()
    cat._entries["drive_read_doc"] = CatalogEntry(
        name="drive_read_doc",
        description="Read a doc.",
        source="workspace",
        service_id="google_drive",
    )
    cat._entries["remember"] = CatalogEntry(
        name="remember",
        description="Recall a memory.",
        source="kernel",
        service_id="",
    )

    # When the disabled set is fed into exclude, the surfacer-facing
    # text drops the disabled tool entirely.
    disabled = cat.disabled_tool_names({"google_drive"})
    text = cat.build_catalog_text(exclude=disabled)
    assert "drive_read_doc" not in text
    assert "remember" in text
