"""Tests for WorkspaceManager.register_stock_tools."""

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.services import (
    ServiceRegistry,
    parse_service_descriptor,
)
from kernos.kernel.tool_catalog import ToolCatalog
from kernos.kernel.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def workspace(tmp_path):
    catalog = ToolCatalog()
    services = ServiceRegistry()
    services.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
    }))
    ws = WorkspaceManager(
        data_dir=str(tmp_path),
        catalog=catalog,
        service_registry=services,
    )
    return ws, catalog


def _write_stock_tool(stock_root: Path, *, service: str, name: str,
                     descriptor_extras: dict | None = None,
                     impl_src: str | None = None):
    service_dir = stock_root / service
    service_dir.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "name": name,
        "description": "stock test tool",
        "input_schema": {"type": "object"},
        "implementation": f"{name}.py",
    }
    if descriptor_extras:
        descriptor.update(descriptor_extras)
    (service_dir / f"{name}.tool.json").write_text(json.dumps(descriptor))
    (service_dir / f"{name}.py").write_text(
        impl_src or "def execute(input_data, context): return {'ok': True}\n"
    )
    return service_dir


def test_register_stock_tools_picks_up_descriptor(tmp_path, workspace):
    ws, catalog = workspace
    stock_root = tmp_path / "stock"
    _write_stock_tool(
        stock_root, service="notion", name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src="def execute(input_data, context): return {'ok': True}\n",
    )
    count = ws.register_stock_tools(stock_root)
    assert count == 1
    entry = catalog.get("reader")
    assert entry is not None
    assert entry.service_id == "notion"
    assert entry.stock_dir == str(stock_root / "notion")
    assert entry.descriptor_file == "reader.tool.json"
    assert entry.implementation == "reader.py"
    assert entry.registration_hash != ""


def test_register_stock_tools_returns_zero_for_missing_dir(tmp_path, workspace):
    ws, _catalog = workspace
    assert ws.register_stock_tools(tmp_path / "does-not-exist") == 0


def test_register_stock_tools_skips_broken_tool_continues_others(tmp_path, workspace, caplog):
    """A broken stock tool should log and skip; valid siblings still register."""
    import logging
    caplog.set_level(logging.WARNING)
    ws, catalog = workspace
    stock_root = tmp_path / "stock"
    # Bad tool: hardcoded path triggers authoring-pattern validator.
    _write_stock_tool(
        stock_root, service="notion", name="bad",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src=(
            "def execute(input_data, context):\n"
            "    open('/etc/passwd').read()\n"
            "    return {}\n"
        ),
    )
    # Good tool.
    _write_stock_tool(
        stock_root, service="notion", name="good",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
    )
    count = ws.register_stock_tools(stock_root)
    assert count == 1
    assert catalog.get("good") is not None
    assert catalog.get("bad") is None
    assert any("STOCK_TOOL_LOAD_FAILED" in r.getMessage() for r in caplog.records)


def test_register_stock_tools_real_notion_descriptor():
    """The shipped notion_read_page tool should register cleanly."""
    catalog = ToolCatalog()
    services = ServiceRegistry()
    # Load the real Notion service descriptor from source.
    services.load_stock_dir(
        Path(__file__).resolve().parent.parent / "kernos" / "kernel" / "services"
    )
    ws = WorkspaceManager(
        data_dir="./data", catalog=catalog, service_registry=services,
    )
    integrations_dir = (
        Path(__file__).resolve().parent.parent
        / "kernos" / "kernel" / "integrations"
    )
    count = ws.register_stock_tools(integrations_dir)
    assert count >= 1
    entry = catalog.get("notion_read_page")
    assert entry is not None
    assert entry.service_id == "notion"
    assert entry.registration_hash != ""
