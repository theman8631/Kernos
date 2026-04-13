"""Tests for SPEC-3B+: MCP Installation.

Covers: connect_one/disconnect_one, secure input mode, credential storage,
config persistence, startup merge, SUPPRESSED status, requires_web_interface.
"""
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream
from kernos.messages.handler import (
    MessageHandler,
    SecureInputState,
    _safe_instance_name,
    resolve_mcp_credentials,
    _SECURE_API_TRIGGER,
)
from kernos.messages.models import NormalizedMessage, AuthLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "discord:1234567890"
CONVERSATION_ID = "discord:1234567890"


def _make_message(content: str, instance_id: str = TENANT_ID) -> NormalizedMessage:
    return NormalizedMessage(
        sender=instance_id.split(":")[1],
        content=content,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=CONVERSATION_ID,
        sender_auth_level=AuthLevel.owner_verified,
        timestamp=datetime.now(timezone.utc),
        instance_id=instance_id,
    )


def _make_cap(name="test-tool", status=CapabilityStatus.AVAILABLE, requires_web=False):
    return CapabilityInfo(
        name=name,
        display_name=name.replace("-", " ").title(),
        description=f"Test capability: {name}",
        category="test",
        status=status,
        server_name=name,
        server_command="echo",
        server_args=[name],
        credentials_key=name,
        env_template={"TEST_KEY": "{credentials}"},
        requires_web_interface=requires_web,
    )


def _make_mock_mcp():
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []
    mcp.get_tool_definitions.return_value = {}
    mcp.connect_one = AsyncMock(return_value=True)
    mcp.disconnect_one = AsyncMock(return_value=True)
    mcp.register_server = MagicMock()
    return mcp


def _make_handler(tmp_path, mcp=None, registry=None):
    """Create a MessageHandler with real stores and minimal mocks for testing."""
    from kernos.kernel.events import JsonEventStream
    from kernos.kernel.state_json import JsonStateStore
    from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore
    from kernos.kernel.reasoning import ReasoningService, Provider
    from kernos.kernel.engine import TaskEngine

    data_dir = str(tmp_path / "data")
    secrets_dir = str(tmp_path / "secrets")

    os.makedirs(data_dir, exist_ok=True)

    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)
    conversations = JsonConversationStore(data_dir)
    tenants = JsonInstanceStore(data_dir)
    audit = JsonAuditStore(data_dir)

    if mcp is None:
        mcp = _make_mock_mcp()

    if registry is None:
        registry = CapabilityRegistry(mcp=mcp)

    mock_provider = AsyncMock(spec=Provider)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)

    handler = MessageHandler(
        mcp=mcp,
        conversations=conversations,
        tenants=tenants,
        audit=audit,
        events=events,
        state=state,
        reasoning=reasoning,
        registry=registry,
        engine=engine,
        secrets_dir=secrets_dir,
    )
    # Override compaction to avoid complex init
    handler.compaction = MagicMock()
    handler.compaction.load_state = AsyncMock(return_value=None)
    handler.compaction.adapter = MagicMock()
    handler.compaction.adapter.count_tokens = AsyncMock(return_value=100)
    handler.compaction.save_state = AsyncMock()
    handler.preference_parsing_enabled = False
    handler.compaction.set_files = MagicMock()

    return handler


# ---------------------------------------------------------------------------
# Component 1: CapabilityStatus.SUPPRESSED
# ---------------------------------------------------------------------------

class TestCapabilityStatusSuppressed:
    def test_suppressed_status_exists(self):
        assert CapabilityStatus.SUPPRESSED == "suppressed"

    def test_suppressed_not_in_get_available(self):
        registry = CapabilityRegistry()
        cap = _make_cap("tool-a", CapabilityStatus.AVAILABLE)
        cap_supp = _make_cap("tool-b", CapabilityStatus.SUPPRESSED)
        registry.register(cap)
        registry.register(cap_supp)
        available = registry.get_available()
        names = [c.name for c in available]
        assert "tool-a" in names
        assert "tool-b" not in names

    def test_suppressed_not_in_build_capability_prompt(self):
        registry = CapabilityRegistry()
        cap_supp = _make_cap("tool-suppressed", CapabilityStatus.SUPPRESSED)
        registry.register(cap_supp)
        prompt = registry.build_capability_prompt()
        assert "tool-suppressed" not in prompt

    def test_suppressed_in_get_all(self):
        registry = CapabilityRegistry()
        cap_supp = _make_cap("tool-b", CapabilityStatus.SUPPRESSED)
        registry.register(cap_supp)
        all_caps = registry.get_all()
        assert any(c.name == "tool-b" for c in all_caps)


# ---------------------------------------------------------------------------
# Component 2: requires_web_interface on CapabilityInfo
# ---------------------------------------------------------------------------

class TestRequiresWebInterface:
    def test_field_exists_default_false(self):
        cap = CapabilityInfo(
            name="test", display_name="Test", description="test",
            category="test", status=CapabilityStatus.AVAILABLE,
        )
        assert cap.requires_web_interface is False

    def test_field_can_be_set_true(self):
        cap = _make_cap("oauth-tool", requires_web=True)
        assert cap.requires_web_interface is True

    def test_google_calendar_requires_web(self):
        from kernos.capability.known import KNOWN_CAPABILITIES
        cal = next(c for c in KNOWN_CAPABILITIES if c.name == "google-calendar")
        assert cal.requires_web_interface is True

    def test_gmail_requires_web(self):
        from kernos.capability.known import KNOWN_CAPABILITIES
        gmail = next(c for c in KNOWN_CAPABILITIES if c.name == "gmail")
        assert gmail.requires_web_interface is True

    def test_server_command_on_known_calendar(self):
        from kernos.capability.known import KNOWN_CAPABILITIES
        cal = next(c for c in KNOWN_CAPABILITIES if c.name == "google-calendar")
        assert cal.server_command == "npx"
        assert "@cocal/google-calendar-mcp" in cal.server_args

    def test_credentials_key_on_known_calendar(self):
        from kernos.capability.known import KNOWN_CAPABILITIES
        cal = next(c for c in KNOWN_CAPABILITIES if c.name == "google-calendar")
        assert cal.credentials_key == "google-calendar"
        assert "GOOGLE_OAUTH_CREDENTIALS" in cal.env_template


# ---------------------------------------------------------------------------
# Component 3: connect_one / disconnect_one
# ---------------------------------------------------------------------------

class TestConnectOne:
    @pytest.fixture
    def mcp(self, tmp_path):
        events = JsonEventStream(str(tmp_path))
        return MCPClientManager(events=events)

    async def test_connect_one_returns_false_if_not_registered(self, mcp):
        result = await mcp.connect_one("nonexistent")
        assert result is False

    async def test_connect_one_returns_true_if_already_connected(self, tmp_path):
        events = JsonEventStream(str(tmp_path))
        mcp = MCPClientManager(events=events)

        # Simulate already connected
        mock_session = MagicMock()
        mcp._sessions["my-server"] = mock_session
        mcp._servers["my-server"] = MagicMock()

        result = await mcp.connect_one("my-server")
        assert result is True

    async def test_connect_one_success(self, tmp_path):
        events = JsonEventStream(str(tmp_path))
        mcp = MCPClientManager(events=events)

        from mcp import StdioServerParameters
        mcp.register_server(
            "test-server",
            StdioServerParameters(command="echo", args=["hello"], env={}),
        )

        tool = MagicMock()
        tool.name = "test-tool"
        tool.description = "A test tool"
        tool.inputSchema = {"type": "object", "properties": {}}

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        list_tools_result = MagicMock()
        list_tools_result.tools = [tool]
        mock_session.list_tools = AsyncMock(return_value=list_tools_result)

        @asynccontextmanager
        async def fake_stdio(params):
            yield MagicMock(), MagicMock()

        @asynccontextmanager
        async def fake_session(r, w):
            yield mock_session

        with patch("kernos.capability.client.stdio_client", fake_stdio), \
             patch("kernos.capability.client.ClientSession", fake_session):
            result = await mcp.connect_one("test-server")

        assert result is True
        assert "test-server" in mcp._sessions
        assert "test-tool" in mcp._tool_to_session
        assert any(t["name"] == "test-tool" for t in mcp._tools)

    async def test_connect_one_failure_returns_false(self, tmp_path):
        from mcp import StdioServerParameters
        events = JsonEventStream(str(tmp_path))
        mcp = MCPClientManager(events=events)
        mcp.register_server("bad-server", StdioServerParameters(command="false", args=[], env={}))

        @asynccontextmanager
        async def failing_stdio(params):
            raise RuntimeError("Connection refused")
            yield  # pragma: no cover

        with patch("kernos.capability.client.stdio_client", failing_stdio):
            result = await mcp.connect_one("bad-server")

        assert result is False
        assert "bad-server" not in mcp._sessions


class TestDisconnectOne:
    async def test_disconnect_one_returns_false_if_not_connected(self, tmp_path):
        mcp = MCPClientManager()
        result = await mcp.disconnect_one("nonexistent")
        assert result is False

    async def test_disconnect_one_removes_session_and_tools(self, tmp_path):
        mcp = MCPClientManager()
        # Simulate a connected server with tools
        mcp._sessions["my-server"] = MagicMock()
        mcp._tool_to_session["tool-a"] = "my-server"
        mcp._tool_to_session["tool-b"] = "other-server"
        mcp._tools = [
            {"name": "tool-a", "description": ""},
            {"name": "tool-b", "description": ""},
        ]

        result = await mcp.disconnect_one("my-server")

        assert result is True
        assert "my-server" not in mcp._sessions
        assert "tool-a" not in mcp._tool_to_session
        assert "tool-b" in mcp._tool_to_session
        assert not any(t["name"] == "tool-a" for t in mcp._tools)
        assert any(t["name"] == "tool-b" for t in mcp._tools)

    async def test_disconnect_one_closes_runtime_stack(self, tmp_path):
        mcp = MCPClientManager()
        mock_stack = MagicMock()
        mock_stack.aclose = AsyncMock()
        mcp._sessions["my-server"] = MagicMock()
        mcp._runtime_stacks["my-server"] = mock_stack
        mcp._tools = []
        mcp._tool_to_session = {}

        await mcp.disconnect_one("my-server")

        mock_stack.aclose.assert_awaited_once()
        assert "my-server" not in mcp._runtime_stacks


# ---------------------------------------------------------------------------
# Component 4: Credential storage
# ---------------------------------------------------------------------------

class TestCredentialStorage:
    async def test_store_credential_writes_file(self, tmp_path):
        handler = _make_handler(tmp_path)
        await handler._store_credential(TENANT_ID, "google-calendar", "my-secret-key")

        safe_name = _safe_instance_name(TENANT_ID)
        secret_path = Path(handler._secrets_dir) / safe_name / "google-calendar.key"
        assert secret_path.exists()
        assert secret_path.read_text() == "my-secret-key"

    async def test_store_credential_sets_permissions(self, tmp_path):
        handler = _make_handler(tmp_path)
        await handler._store_credential(TENANT_ID, "test-tool", "secret")

        safe_name = _safe_instance_name(TENANT_ID)
        secret_path = Path(handler._secrets_dir) / safe_name / "test-tool.key"
        mode = oct(secret_path.stat().st_mode)[-3:]
        assert mode == "600"

    def test_resolve_mcp_credentials_injects_value(self, tmp_path):
        secrets_dir = str(tmp_path / "secrets")
        safe = _safe_instance_name(TENANT_ID)
        key_dir = Path(secrets_dir) / safe
        key_dir.mkdir(parents=True)
        (key_dir / "my-tool.key").write_text("actual-api-key")

        server_config = {
            "credentials_key": "my-tool",
            "env_template": {"MY_API_KEY": "{credentials}"},
        }
        result = resolve_mcp_credentials(server_config, TENANT_ID, secrets_dir)
        assert result["MY_API_KEY"] == "actual-api-key"

    def test_resolve_mcp_credentials_falls_back_to_env(self, tmp_path, monkeypatch):
        secrets_dir = str(tmp_path / "secrets")
        monkeypatch.setenv("MY_API_KEY", "env-fallback-key")

        server_config = {
            "credentials_key": "nonexistent-tool",
            "env_template": {"MY_API_KEY": "{credentials}"},
        }
        result = resolve_mcp_credentials(server_config, TENANT_ID, secrets_dir)
        assert result["MY_API_KEY"] == "env-fallback-key"

    def test_resolve_mcp_credentials_literal_template(self, tmp_path):
        secrets_dir = str(tmp_path / "secrets")
        server_config = {
            "credentials_key": "",
            "env_template": {"STATIC_VAR": "literal-value"},
        }
        result = resolve_mcp_credentials(server_config, TENANT_ID, secrets_dir)
        assert result["STATIC_VAR"] == "literal-value"

    def test_credential_safe_name_sanitizes_colon(self):
        result = _safe_instance_name("discord:1234567890")
        assert ":" not in result

    async def test_credential_preserved_on_disconnect(self, tmp_path):
        """Credentials in secrets/ are preserved after disconnect (AC 14)."""
        handler = _make_handler(tmp_path)
        # Store a credential
        await handler._store_credential(TENANT_ID, "my-tool", "my-key")

        # Disconnect the capability
        await handler._disconnect_capability(TENANT_ID, "my-tool")

        # Credential file still exists
        safe_name = _safe_instance_name(TENANT_ID)
        secret_path = Path(handler._secrets_dir) / safe_name / "my-tool.key"
        assert secret_path.exists()
        assert secret_path.read_text() == "my-key"


# ---------------------------------------------------------------------------
# Component 5: Secure input mode
# ---------------------------------------------------------------------------

class TestSecureInputMode:
    async def test_secure_api_trigger_activates_mode(self, tmp_path):
        """AC 3: 'secure api' → handler returns secure mode message."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("google-calendar")
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        # Pre-populate secure input inference: mock _infer_pending_capability
        handler._infer_pending_capability = AsyncMock(return_value="google-calendar")

        msg = _make_message("secure api")
        response = await handler.process(msg)

        assert "Secure input mode active" in response
        assert "google-calendar" in response
        assert "NOT be seen by any agent" in response
        assert TENANT_ID in handler._secure_input_state

    async def test_credential_message_bypasses_pipeline(self, tmp_path):
        """AC 3, 5: Credential message goes to storage, never LLM."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        # Manually set secure input state
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

        # Mock _connect_after_credential
        handler._connect_after_credential = AsyncMock(return_value=False)
        handler._store_credential = AsyncMock()

        msg = _make_message("sk-my-super-secret-api-key-12345")
        response = await handler.process(msg)

        # State is cleared
        assert TENANT_ID not in handler._secure_input_state
        # Credential was stored
        handler._store_credential.assert_awaited_once_with(
            TENANT_ID, "test-tool", "sk-my-super-secret-api-key-12345"
        )
        # Response is connection status, not normal agent response
        assert "Key stored" in response

    async def test_secure_mode_success_response(self, tmp_path):
        """Successful connection after credential."""
        handler = _make_handler(tmp_path)
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        handler._connect_after_credential = AsyncMock(return_value=True)
        handler._store_credential = AsyncMock()

        msg = _make_message("my-api-key")
        response = await handler.process(msg)

        assert "now connected" in response
        assert "test-tool" in response

    async def test_secure_mode_failure_response(self, tmp_path):
        """Failed connection after credential."""
        handler = _make_handler(tmp_path)
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        handler._connect_after_credential = AsyncMock(return_value=False)
        handler._store_credential = AsyncMock()

        msg = _make_message("bad-api-key")
        response = await handler.process(msg)

        assert "Key stored" in response
        assert "couldn't connect" in response

    async def test_timeout_clears_state_and_notifies(self, tmp_path):
        """AC 6, 7: Timeout → message processed normally, user notified."""
        handler = _make_handler(tmp_path)
        # Expired state
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        msg = _make_message("this is not a credential")
        response = await handler.process(msg)

        assert TENANT_ID not in handler._secure_input_state
        assert "timed out" in response.lower()
        assert "10 minutes" in response

    async def test_secure_api_no_capability_inferred(self, tmp_path):
        """'secure api' with no pending capability → helpful error."""
        handler = _make_handler(tmp_path)
        handler._infer_pending_capability = AsyncMock(return_value=None)

        msg = _make_message("secure api")
        response = await handler.process(msg)

        assert "not sure which tool" in response.lower()
        assert TENANT_ID not in handler._secure_input_state

    async def test_credential_not_in_conversation_store(self, tmp_path):
        """AC 5: The credential message must not appear in any conversation store."""
        handler = _make_handler(tmp_path)
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        handler._connect_after_credential = AsyncMock(return_value=True)
        handler._store_credential = AsyncMock()

        secret_key = "sk-super-secret-api-key-that-must-never-be-stored"
        msg = _make_message(secret_key)
        await handler.process(msg)

        # Check conversation store does not contain the secret
        stored = await handler.conversations.get_recent(TENANT_ID, CONVERSATION_ID, limit=50)
        for entry in stored:
            assert secret_key not in str(entry.get("content", ""))


# ---------------------------------------------------------------------------
# Component 6: Config persistence
# ---------------------------------------------------------------------------

class TestConfigPersistence:
    async def test_persist_mcp_config_writes_file(self, tmp_path):
        """_persist_mcp_config writes mcp-servers.json to system space."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = CapabilityInfo(
            name="test-tool", display_name="Test Tool", description="test",
            category="test", status=CapabilityStatus.CONNECTED,
            server_name="test-tool", server_command="npx",
            server_args=["test-mcp"], credentials_key="test-tool",
            env_template={"TEST_KEY": "{credentials}"}, universal=False,
        )
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        # Create system space
        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID,
            name="System",
            description="System",
            space_type="system",
            status="active",
            is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        await handler._persist_mcp_config(TENANT_ID)

        # Read back
        config_raw = await handler._files.read_file(TENANT_ID, system_space.id, "mcp-servers.json")
        assert not config_raw.startswith("Error:")
        config = json.loads(config_raw)
        assert "test-tool" in config["servers"]
        assert config["servers"]["test-tool"]["command"] == "npx"

    async def test_persist_mcp_config_includes_suppressed_in_uninstalled(self, tmp_path):
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("suppressed-tool", CapabilityStatus.SUPPRESSED)
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID, name="System", description="System",
            space_type="system", status="active", is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        await handler._persist_mcp_config(TENANT_ID)

        config_raw = await handler._files.read_file(TENANT_ID, system_space.id, "mcp-servers.json")
        config = json.loads(config_raw)
        assert "suppressed-tool" in config["uninstalled"]

    async def test_maybe_load_mcp_config_suppresses_uninstalled(self, tmp_path):
        """AC 9: Uninstalled entries are suppressed on load."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("old-tool", CapabilityStatus.AVAILABLE)
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID, name="System", description="System",
            space_type="system", status="active", is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        # Write config with old-tool in uninstalled list
        config = {"servers": {}, "uninstalled": ["old-tool"]}
        await handler._files.write_file(
            TENANT_ID, system_space.id, "mcp-servers.json",
            json.dumps(config), "test"
        )

        await handler._maybe_load_mcp_config(TENANT_ID)

        loaded_cap = handler.registry.get("old-tool")
        assert loaded_cap is not None
        assert loaded_cap.status == CapabilityStatus.SUPPRESSED

    async def test_maybe_load_mcp_config_only_runs_once(self, tmp_path):
        """Config is only loaded once per_instance per process lifetime."""
        handler = _make_handler(tmp_path)
        handler._get_system_space = AsyncMock(return_value=None)

        await handler._maybe_load_mcp_config(TENANT_ID)
        await handler._maybe_load_mcp_config(TENANT_ID)

        # Second call should not call _get_system_space again
        handler._get_system_space.assert_awaited_once()

    async def test_maybe_load_mcp_config_connects_persisted_servers(self, tmp_path):
        """AC 8: Persisted servers connect on startup (restart simulation)."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("my-tool", CapabilityStatus.AVAILABLE)
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID, name="System", description="System",
            space_type="system", status="active", is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        config = {
            "servers": {
                "my-tool": {
                    "display_name": "My Tool",
                    "command": "npx",
                    "args": ["my-mcp"],
                    "credentials_key": "my-tool",
                    "env_template": {"MY_KEY": "{credentials}"},
                    "universal": False,
                    "tool_effects": {},
                }
            },
            "uninstalled": [],
        }
        await handler._files.write_file(
            TENANT_ID, system_space.id, "mcp-servers.json",
            json.dumps(config), "test"
        )

        # Mock connect_one to return success and update tools
        mcp.connect_one = AsyncMock(return_value=True)
        mcp.get_tool_definitions = MagicMock(return_value={"my-tool": [{"name": "do-thing"}]})

        await handler._maybe_load_mcp_config(TENANT_ID)

        mcp.register_server.assert_called()
        mcp.connect_one.assert_awaited_with("my-tool")
        loaded_cap = handler.registry.get("my-tool")
        assert loaded_cap.status == CapabilityStatus.CONNECTED


# ---------------------------------------------------------------------------
# Component 7: Events
# ---------------------------------------------------------------------------

class TestInstallEvents:
    def test_tool_installed_event_type_exists(self):
        assert EventType.TOOL_INSTALLED == "tool.installed"

    def test_tool_uninstalled_event_type_exists(self):
        assert EventType.TOOL_UNINSTALLED == "tool.uninstalled"

    async def test_connect_after_credential_emits_installed_event(self, tmp_path):
        """AC 12: TOOL_INSTALLED event emitted after successful connection."""
        mcp = _make_mock_mcp()
        mcp.get_tool_definitions = MagicMock(return_value={"test-tool": [{"name": "do-thing"}]})
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("test-tool", CapabilityStatus.AVAILABLE)
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)
        handler._persist_mcp_config = AsyncMock()
        handler._write_capabilities_overview = AsyncMock()
        handler._get_system_space = AsyncMock(return_value=None)

        await handler._connect_after_credential(TENANT_ID, "test-tool")

        # Check event was emitted
        data_dir = str(tmp_path / "data")
        import glob
        event_files = glob.glob(f"{data_dir}/{_safe_instance_name(TENANT_ID)}/events/*.json")
        found = False
        for ef in event_files:
            with open(ef) as f:
                events = json.load(f)
            for evt in events:
                if evt.get("type") == "tool.installed":
                    found = True
                    assert evt["payload"]["capability_name"] == "test-tool"
                    break
        assert found, "tool.installed event not found in event stream"

    async def test_disconnect_capability_emits_uninstalled_event(self, tmp_path):
        """AC 13: TOOL_UNINSTALLED event emitted after disconnection."""
        mcp = _make_mock_mcp()
        mcp.disconnect_one = AsyncMock(return_value=True)
        registry = CapabilityRegistry(mcp=mcp)
        cap = _make_cap("test-tool", CapabilityStatus.CONNECTED)
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)
        handler._persist_mcp_config = AsyncMock()
        handler._write_capabilities_overview = AsyncMock()
        handler._get_system_space = AsyncMock(return_value=None)

        await handler._disconnect_capability(TENANT_ID, "test-tool")

        data_dir = str(tmp_path / "data")
        import glob
        event_files = glob.glob(f"{data_dir}/{_safe_instance_name(TENANT_ID)}/events/*.json")
        found = False
        for ef in event_files:
            with open(ef) as f:
                events = json.load(f)
            for evt in events:
                if evt.get("type") == "tool.uninstalled":
                    found = True
                    assert evt["payload"]["capability_name"] == "test-tool"
                    break
        assert found, "tool.uninstalled event not found in event stream"


# ---------------------------------------------------------------------------
# Component 8: capabilities-overview.md refresh
# ---------------------------------------------------------------------------

class TestCapabilitiesOverviewRefresh:
    async def test_write_capabilities_overview_connected(self, tmp_path):
        """AC 11: capabilities-overview.md reflects newly connected tool."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap = CapabilityInfo(
            name="my-tool", display_name="My Tool", description="test",
            category="test", status=CapabilityStatus.CONNECTED,
            server_name="my-tool", tools=["do-thing"],
        )
        registry.register(cap)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID, name="System", description="System",
            space_type="system", status="active", is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        await handler._write_capabilities_overview(TENANT_ID, system_space.id)

        content = await handler._files.read_file(TENANT_ID, system_space.id, "capabilities-overview.md")
        assert "my-tool" in content
        assert "do-thing" in content

    async def test_write_capabilities_overview_after_disconnect(self, tmp_path):
        """After disconnect, overview no longer shows the tool as connected."""
        mcp = _make_mock_mcp()
        registry = CapabilityRegistry(mcp=mcp)
        cap_connected = CapabilityInfo(
            name="tool-a", display_name="Tool A", description="test",
            category="test", status=CapabilityStatus.CONNECTED,
            server_name="tool-a",
        )
        cap_suppressed = _make_cap("tool-b", CapabilityStatus.SUPPRESSED)
        registry.register(cap_connected)
        registry.register(cap_suppressed)
        handler = _make_handler(tmp_path, mcp=mcp, registry=registry)

        import uuid
        from kernos.kernel.spaces import ContextSpace
        system_space = ContextSpace(
            id=f"space_{uuid.uuid4().hex[:8]}",
            instance_id=TENANT_ID, name="System", description="System",
            space_type="system", status="active", is_default=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        await handler.state.save_context_space(system_space)

        await handler._write_capabilities_overview(TENANT_ID, system_space.id)

        content = await handler._files.read_file(TENANT_ID, system_space.id, "capabilities-overview.md")
        assert "tool-a" in content
        # Suppressed tool not in available section
        assert "tool-b" not in content.lower() or "suppressed" not in content.lower()


# ---------------------------------------------------------------------------
# Component 9: System prompt includes secure api script
# ---------------------------------------------------------------------------

class TestSystemPromptScript:
    async def test_system_space_posture_includes_secure_api(self, tmp_path):
        """AC 16: System space posture instructs agent to use 'secure api' flow."""
        handler = _make_handler(tmp_path)
        # Trigger soul/space creation
        await handler._get_or_init_soul(TENANT_ID)

        system_space = await handler._get_system_space(TENANT_ID)
        assert system_space is not None
        assert "secure api" in system_space.posture.lower()
        assert "NEVER" in system_space.posture or "never" in system_space.posture.lower()

    async def test_system_space_posture_api_key_warning(self, tmp_path):
        """System space posture must not allow direct API key pasting."""
        handler = _make_handler(tmp_path)
        await handler._get_or_init_soul(TENANT_ID)

        system_space = await handler._get_system_space(TENANT_ID)
        assert "API key" in system_space.posture or "api key" in system_space.posture.lower()


# ---------------------------------------------------------------------------
# Component 10: SecureInputState dataclass
# ---------------------------------------------------------------------------

class TestSecureInputState:
    def test_secure_input_state_fields(self):
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        state = SecureInputState(capability_name="test-tool", expires_at=expires)
        assert state.capability_name == "test-tool"
        assert state.expires_at == expires

    def test_secure_api_trigger_constant(self):
        assert _SECURE_API_TRIGGER == "secure api"

    async def test_state_cleared_after_credential(self, tmp_path):
        handler = _make_handler(tmp_path)
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="test-tool",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        handler._store_credential = AsyncMock()
        handler._connect_after_credential = AsyncMock(return_value=True)

        msg = _make_message("my-key")
        await handler.process(msg)

        assert TENANT_ID not in handler._secure_input_state
