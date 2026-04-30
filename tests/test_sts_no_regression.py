"""STS C3 — no-regression pin (AC #24).

After STS lands, the foundational invariants of the substrate it
composes against must still hold:

* WDP (workflow drafts) — atomic create/patch/transitions, lifecycle
  states unchanged, sidecar still wired.
* DAR (agent registry) — instance scoping, alias collision, status
  lifecycle.
* WLP (workflow registry) — atomic register, agent reference
  validation, instance scoping; the rename to
  ``_register_workflow_unbound`` is internal and does not weaken any
  invariant.
* event_stream — fire-and-forget, durable persistence, post-flush
  hooks; envelope addition is additive.

The detailed pins live in their respective test suites. This test
asserts the public-API surfaces still exist with the expected shapes
so a future refactor cannot quietly remove them.
"""
from __future__ import annotations

import inspect

from kernos.kernel import event_stream
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


class TestWDPSurfaceSurvives:
    def test_create_draft_signature(self):
        sig = inspect.signature(DraftRegistry.create_draft)
        params = sig.parameters
        # Keyword-only required: instance_id, intent_summary.
        assert params["instance_id"].kind == inspect.Parameter.KEYWORD_ONLY
        assert params["intent_summary"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_list_drafts_signature(self):
        sig = inspect.signature(DraftRegistry.list_drafts)
        params = sig.parameters
        for name in ("instance_id", "status", "home_space_id", "include_terminal"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY


class TestDARSurfaceSurvives:
    def test_register_agent_present(self):
        assert inspect.iscoroutinefunction(AgentRegistry.register_agent)

    def test_get_by_id_signature(self):
        sig = inspect.signature(AgentRegistry.get_by_id)
        params = list(sig.parameters)
        # (self, agent_id, instance_id) — composite-key lookup.
        assert params == ["self", "agent_id", "instance_id"]

    def test_list_agents_signature(self):
        sig = inspect.signature(AgentRegistry.list_agents)
        assert "instance_id" in sig.parameters


class TestWLPSurfaceSurvives:
    def test_register_workflow_renamed_not_removed(self):
        # AC #13: the public method is renamed to underscore-prefixed
        # but the underlying capability is unchanged.
        assert hasattr(WorkflowRegistry, "_register_workflow_unbound")
        assert not hasattr(WorkflowRegistry, "register_workflow"), (
            "AC #13 invariant: WLP's public register_workflow is "
            "renamed; cohort/CRB code paths must not import the legacy "
            "name"
        )

    def test_register_workflow_from_file_present(self):
        """Bootstrap path remains; production callers use STS."""
        assert callable(getattr(WorkflowRegistry, "register_workflow_from_file", None))

    def test_get_workflow_signature(self):
        sig = inspect.signature(WorkflowRegistry.get_workflow)
        params = list(sig.parameters)
        # (self, workflow_id) — instance scope is post-fetch in STS.
        assert "workflow_id" in params

    def test_list_workflows_signature(self):
        sig = inspect.signature(WorkflowRegistry.list_workflows)
        assert "instance_id" in sig.parameters

    def test_unbound_method_takes_approval_event_id(self):
        sig = inspect.signature(WorkflowRegistry._register_workflow_unbound)
        params = sig.parameters
        assert "approval_event_id" in params
        assert params["approval_event_id"].kind == inspect.Parameter.KEYWORD_ONLY


class TestEventStreamSurfaceSurvives:
    def test_emit_signature_unchanged(self):
        sig = inspect.signature(event_stream.emit)
        # Legacy callers continue to work — keyword args are unchanged.
        for name in ("instance_id", "event_type", "payload",
                     "member_id", "space_id", "correlation_id"):
            assert name in sig.parameters

    def test_event_by_id_added(self):
        # New read API for STS approval lookup.
        assert hasattr(event_stream, "event_by_id")
        assert inspect.iscoroutinefunction(event_stream.event_by_id)

    def test_emitter_registry_singleton(self):
        # New EmitterRegistry surface added without removing emit().
        assert callable(getattr(event_stream, "emitter_registry", None))
        assert hasattr(event_stream, "EmitterRegistry")
        assert hasattr(event_stream, "EventEmitter")

    def test_post_flush_hook_surface_unchanged(self):
        # The post-flush hook contract from the C0 of WORKFLOW-LOOP-PRIMITIVE
        # must survive — workflow execution depends on it.
        assert callable(event_stream.register_post_flush_hook)
        assert callable(event_stream.unregister_post_flush_hook)


class TestSchemaInvariants:
    """The substrate's persistence invariants survive STS schema changes."""

    async def test_workflows_table_still_has_core_columns(self, tmp_path):
        """STS adds approval_event_id; everything else stays."""
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        from kernos.kernel.workflows.trigger_registry import (
            TriggerRegistry,
            _reset_for_tests as _reset_trigger_registry,
        )
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        try:
            async with wfr._db.execute("PRAGMA table_info(workflows)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            for required in (
                "workflow_id", "instance_id", "name", "description",
                "owner", "version", "status", "descriptor_json",
                "created_at", "approval_event_id",
            ):
                assert required in cols, f"missing column: {required}"
        finally:
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()

    async def test_events_table_still_has_core_columns(self, tmp_path):
        """STS adds source_module; everything else stays."""
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            db = await event_stream._WRITER.read_db()
            assert db is not None
            async with db.execute("PRAGMA table_info(events)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            for required in (
                "event_id", "instance_id", "member_id", "space_id",
                "timestamp", "event_type", "payload", "correlation_id",
                "source_module",
            ):
                assert required in cols, f"missing column: {required}"
        finally:
            await event_stream._reset_for_tests()
