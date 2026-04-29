"""Tests for ProviderRegistry + atomic register_agent + alias
collision check.

DOMAIN-AGENT-REGISTRY C2. Pins:

  - ProviderRegistry register / get / has / construct semantics
  - register_agent atomic flow: validation fails leave no partial
    state (AC #2)
  - alias collision rejected at registration with field-level
    error naming both agents (AC #3, fail-closed)
  - lifecycle: active / paused / retired transitions, retired is
    terminal
  - composite-PK collision translates to typed
    AgentRegistryError (not raw SQL)
  - provider_key unknown → AgentInboxProviderUnavailable at
    registration time
"""
from __future__ import annotations

import pytest

from kernos.kernel.agents.providers import (
    ProviderKeyUnknown,
    ProviderRegistry,
)
from kernos.kernel.agents.registry import (
    AgentInboxProviderUnavailable,
    AgentNotRegistered,
    AgentRecord,
    AgentRegistry,
    AgentRegistryError,
    AliasCollisionError,
    InvalidAgentStatusTransition,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox


def _record(**overrides) -> AgentRecord:
    base = dict(
        agent_id="spec-agent",
        instance_id="inst_a",
        display_name="Spec drafting agent",
        aliases=[],
        provider_key="inmemory",
        provider_config_ref="inbox-1",
        domain_summary="Drafts specs.",
        capabilities_summary="Spec writing + review.",
        status="active",
        version=1,
    )
    base.update(overrides)
    return AgentRecord(**base)


def _provider_registry_with_inmem() -> ProviderRegistry:
    pr = ProviderRegistry()
    pr.register("inmemory", lambda config_ref: InMemoryAgentInbox())
    return pr


@pytest.fixture
async def registry(tmp_path):
    pr = _provider_registry_with_inmem()
    reg = AgentRegistry(provider_registry=pr)
    await reg.start(str(tmp_path))
    yield reg
    await reg.stop()


# ===========================================================================
# ProviderRegistry
# ===========================================================================


class TestProviderRegistry:
    def test_register_and_get(self):
        pr = ProviderRegistry()
        factory = lambda config_ref: InMemoryAgentInbox()
        pr.register("inmemory", factory)
        assert pr.has("inmemory")
        assert pr.get("inmemory") is factory

    def test_construct_returns_concrete_inbox(self):
        pr = _provider_registry_with_inmem()
        inbox = pr.construct("inmemory", "any-config-ref")
        assert isinstance(inbox, InMemoryAgentInbox)

    def test_construct_unknown_key_raises_typed_error(self):
        pr = ProviderRegistry()
        with pytest.raises(ProviderKeyUnknown) as exc_info:
            pr.construct("notion", "config")
        assert exc_info.value.provider_key == "notion"

    def test_register_duplicate_rejected(self):
        pr = ProviderRegistry()
        pr.register("k", lambda c: InMemoryAgentInbox())
        with pytest.raises(ValueError, match="already registered"):
            pr.register("k", lambda c: InMemoryAgentInbox())

    def test_unregister_round_trip(self):
        pr = _provider_registry_with_inmem()
        assert pr.unregister("inmemory") is True
        assert pr.unregister("inmemory") is False

    def test_register_empty_key_rejected(self):
        pr = ProviderRegistry()
        with pytest.raises(ValueError, match="non-empty"):
            pr.register("", lambda c: InMemoryAgentInbox())

    def test_known_keys(self):
        pr = ProviderRegistry()
        pr.register("a", lambda c: InMemoryAgentInbox())
        pr.register("b", lambda c: InMemoryAgentInbox())
        assert set(pr.known_keys()) == {"a", "b"}


# ===========================================================================
# Atomic register_agent (AC #2)
# ===========================================================================


class TestAtomicRegistration:
    async def test_register_agent_round_trip(self, registry):
        record = _record(aliases=["spec drafter"])
        out = await registry.register_agent(record)
        assert out.created_at  # auto-filled
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is not None
        assert loaded.aliases == ["spec drafter"]

    async def test_unknown_provider_key_rejected(self, registry):
        # provider_registry only has "inmemory".
        record = _record(provider_key="notion")
        with pytest.raises(AgentInboxProviderUnavailable):
            await registry.register_agent(record)
        # No row persisted.
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is None

    async def test_unknown_status_rejected(self, registry):
        record = _record(status="weird")
        with pytest.raises(InvalidAgentStatusTransition):
            await registry.register_agent(record)
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is None

    async def test_missing_provider_key_rejected(self, registry):
        record = _record(provider_key="")
        with pytest.raises(ValueError, match="provider_key"):
            await registry.register_agent(record)

    async def test_duplicate_agent_id_translates_to_typed_error(
        self, registry,
    ):
        await registry.register_agent(_record())
        # Second registration with same composite key.
        with pytest.raises(AgentRegistryError, match="already registered"):
            await registry.register_agent(_record())

    async def test_no_provider_registry_skips_provider_validation(
        self, tmp_path,
    ):
        """Constructor without ProviderRegistry → register_agent
        skips the provider_key validation step. Useful for tests
        that don't care about dispatch."""
        reg = AgentRegistry()  # no provider_registry
        await reg.start(str(tmp_path))
        try:
            record = _record(provider_key="anything")
            out = await reg.register_agent(record)
            assert out.agent_id == "spec-agent"
        finally:
            await reg.stop()


# ===========================================================================
# Alias collision (AC #3, fail-closed)
# ===========================================================================


class TestAliasCollision:
    async def test_collision_with_active_record_rejected(self, registry):
        await registry.register_agent(_record(
            agent_id="agent-a", aliases=["reviewer", "code review"],
        ))
        # Second registration with overlapping alias.
        with pytest.raises(AliasCollisionError) as exc_info:
            await registry.register_agent(_record(
                agent_id="agent-b", aliases=["reviewer"],
            ))
        assert exc_info.value.alias == "reviewer"
        assert exc_info.value.conflicting_agent_id == "agent-a"
        assert exc_info.value.attempting_agent_id == "agent-b"
        # No partial state — agent-b not persisted.
        loaded = await registry.get_by_id("agent-b", "inst_a")
        assert loaded is None

    async def test_collision_with_paused_record_allowed(self, registry):
        """Aliases are only collision-checked against ACTIVE
        records. A paused agent's aliases can be re-claimed by a
        new registration — operator workflow when migrating
        agents."""
        await registry.register_agent(_record(
            agent_id="agent-a", aliases=["reviewer"],
        ))
        await registry.update_status("agent-a", "inst_a", "paused")
        # New active record claims the alias — allowed.
        await registry.register_agent(_record(
            agent_id="agent-b", aliases=["reviewer"],
        ))
        b = await registry.get_by_id("agent-b", "inst_a")
        assert b is not None
        assert b.aliases == ["reviewer"]

    async def test_collision_check_scoped_to_instance(self, registry):
        """Alias collision check walks records in the SAME instance;
        instance B may freely claim instance A's aliases."""
        await registry.register_agent(_record(
            agent_id="agent-a", instance_id="inst_a", aliases=["reviewer"],
        ))
        # Same alias in different instance — fine.
        await registry.register_agent(_record(
            agent_id="agent-b", instance_id="inst_b", aliases=["reviewer"],
        ))
        a = await registry.get_by_id("agent-a", "inst_a")
        b = await registry.get_by_id("agent-b", "inst_b")
        assert a is not None and b is not None

    async def test_no_aliases_no_collision_check(self, registry):
        await registry.register_agent(_record(
            agent_id="agent-a", aliases=[],
        ))
        await registry.register_agent(_record(
            agent_id="agent-b", aliases=[],
        ))


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestLifecycleTransitions:
    async def test_active_to_paused_round_trip(self, registry):
        await registry.register_agent(_record())
        out = await registry.update_status("spec-agent", "inst_a", "paused")
        assert out is not None
        assert out.status == "paused"

    async def test_paused_to_active_allowed(self, registry):
        await registry.register_agent(_record(status="paused"))
        out = await registry.update_status("spec-agent", "inst_a", "active")
        assert out.status == "active"

    async def test_active_to_retired_terminal(self, registry):
        await registry.register_agent(_record())
        await registry.update_status("spec-agent", "inst_a", "retired")
        # retired → active forbidden.
        with pytest.raises(InvalidAgentStatusTransition,
                           match="terminal"):
            await registry.update_status(
                "spec-agent", "inst_a", "active",
            )
        # retired → retired allowed (no-op).
        out = await registry.update_status(
            "spec-agent", "inst_a", "retired",
        )
        assert out.status == "retired"

    async def test_unknown_status_rejected(self, registry):
        await registry.register_agent(_record())
        with pytest.raises(InvalidAgentStatusTransition):
            await registry.update_status(
                "spec-agent", "inst_a", "weird",
            )

    async def test_update_unknown_agent_raises(self, registry):
        with pytest.raises(AgentNotRegistered):
            await registry.update_status(
                "nonexistent", "inst_a", "paused",
            )

    async def test_version_bumped_on_status_change(self, registry):
        await registry.register_agent(_record())
        before = await registry.get_by_id("spec-agent", "inst_a")
        await registry.update_status("spec-agent", "inst_a", "paused")
        after = await registry.get_by_id("spec-agent", "inst_a")
        assert after.version == before.version + 1
