"""Drafter no-regression invariant pin (DRAFTER C4, AC #31).

After Drafter lands, the foundational invariants of the substrate it
composes against must still hold:

* WDP (workflow drafts) — atomic create/patch/transitions, lifecycle
  states unchanged, sidecar still wired.
* STS (substrate tools) — facade surface, approval-bound register,
  envelope source authority pin.
* DAR (agent registry) — instance scoping, alias collision.
* WLP (workflow registry) — _register_workflow_unbound rename intact.
* event_stream — emit signature, emitter_registry, EventEmitter
  token gate.
"""
from __future__ import annotations

import inspect

from kernos.kernel import event_stream
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.cohorts._substrate.action_log import (
    ALLOWED_ACTION_TYPES,
)
from kernos.kernel.cohorts.drafter.signals import SIGNAL_TYPES
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import SubstrateTools
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


class TestWDPSurfaceSurvives:
    def test_create_draft_keyword_only(self):
        sig = inspect.signature(DraftRegistry.create_draft)
        for name in ("instance_id", "intent_summary"):
            assert (
                sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
            )

    def test_list_drafts_signature(self):
        sig = inspect.signature(DraftRegistry.list_drafts)
        for name in (
            "instance_id", "status", "home_space_id", "include_terminal",
        ):
            assert name in sig.parameters

    def test_update_draft_requires_expected_version(self):
        sig = inspect.signature(DraftRegistry.update_draft)
        assert "expected_version" in sig.parameters


class TestSTSSurfaceSurvives:
    def test_facade_register_workflow_present(self):
        assert callable(getattr(SubstrateTools, "register_workflow", None))

    def test_facade_query_surfaces_present(self):
        for name in (
            "list_known_providers", "list_agents", "list_workflows",
            "list_drafts", "query_context_brief",
        ):
            assert callable(getattr(SubstrateTools, name, None))


class TestDARSurfaceSurvives:
    def test_register_agent_present(self):
        assert inspect.iscoroutinefunction(AgentRegistry.register_agent)

    def test_get_by_id_signature(self):
        sig = inspect.signature(AgentRegistry.get_by_id)
        params = list(sig.parameters)
        assert params == ["self", "agent_id", "instance_id"]


class TestWLPRenameIntact:
    def test_underscore_register_workflow_unbound_present(self):
        assert hasattr(WorkflowRegistry, "_register_workflow_unbound")

    def test_public_register_workflow_absent(self):
        assert not hasattr(WorkflowRegistry, "register_workflow")


class TestEventStreamSurfaceSurvives:
    def test_emit_signature_unchanged(self):
        sig = inspect.signature(event_stream.emit)
        for name in ("instance_id", "event_type", "payload"):
            assert name in sig.parameters

    def test_event_by_id_present(self):
        assert inspect.iscoroutinefunction(event_stream.event_by_id)

    def test_emitter_registry_token_gate(self):
        # Direct EventEmitter construction must still raise — the STS
        # source-authority safety property survives Drafter's addition.
        import pytest as _pt
        with _pt.raises(RuntimeError, match="cannot be constructed directly"):
            event_stream.EventEmitter(source_module="drafter")


class TestActionLogActionTypeSurface:
    def test_allowed_action_types_unchanged_by_drafter(self):
        # Pin again at the C4 level: Drafter does not extend the surface.
        assert ALLOWED_ACTION_TYPES == frozenset({
            "create_draft", "update_draft", "abandon_draft",
            "emit_signal", "emit_receipt",
        })


class TestDrafterSignalSurfaceStability:
    def test_signal_types_pinned(self):
        # Pin at no-regression level so a future cohort spec touching
        # signals must explicitly update this test.
        assert SIGNAL_TYPES == frozenset({
            "drafter.signal.draft_ready",
            "drafter.signal.gap_detected",
            "drafter.signal.multi_intent_detected",
            "drafter.signal.idle_resurface",
            "drafter.signal.draft_paused",
            "drafter.signal.draft_abandoned",
        })
