"""CRB no-regression invariant pin (CRB C6, AC #38).

After CRB main lands, the foundational invariants of every preceding
spec must still hold:

* WDP (drafts): atomic CRUD, lifecycle, sidecar.
* STS: facade surface, approval-bound register, envelope source
  authority, find_workflow_by_approval_event_id.
* DAR: instance scoping, alias collision.
* WLP: rename intact (_register_workflow_unbound).
* event_stream: emit returns event_id; envelope substrate-set;
  EmitterRegistry uniqueness.
* Drafter v2: cohort_id pinned; subscribed event types pinned; port
  structural absence; action_log claim-first protocol.
* Drafter v1.1: receipt feedback_received in RECEIPT_TYPES;
  crb.feedback.modify_request in SUBSCRIBED_EVENT_TYPES.
* Drafter v1.2 (CRB C1 inline): CandidateIntent dataclass single
  source of truth.
"""
from __future__ import annotations

import inspect

from kernos.kernel import event_stream
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.cohorts._substrate.action_log import (
    ALLOWED_ACTION_TYPES,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PERFORMED,
)
from kernos.kernel.cohorts.drafter import (
    COHORT_ID,
    SUBSCRIBED_EVENT_TYPES,
)
from kernos.kernel.cohorts.drafter.ports import DrafterDraftPort
from kernos.kernel.cohorts.drafter.receipts import RECEIPT_TYPES
from kernos.kernel.cohorts.drafter.signals import (
    CandidateIntent,
    SIGNAL_TYPES,
)
from kernos.kernel.crb.events import CRB_EVENT_TYPES, CRB_SOURCE_MODULE
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

    def test_update_draft_requires_expected_version(self):
        sig = inspect.signature(DraftRegistry.update_draft)
        assert "expected_version" in sig.parameters


class TestSTSSurfaceSurvives:
    def test_facade_register_workflow_present(self):
        assert callable(getattr(SubstrateTools, "register_workflow", None))

    def test_find_workflow_by_approval_event_id_present(self):
        assert inspect.iscoroutinefunction(
            SubstrateTools.find_workflow_by_approval_event_id,
        )

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
    def test_emit_returns_event_id(self):
        """CRB C5 added a return value. Existing callers ignoring
        the return continue to work; new callers (CRB) get the
        substrate-set event_id."""
        sig = inspect.signature(event_stream.emit)
        assert sig.return_annotation == "str"

    def test_event_by_id_present(self):
        assert inspect.iscoroutinefunction(event_stream.event_by_id)

    def test_emitter_registry_token_gate(self):
        # Direct EventEmitter construction must still raise — the STS
        # source-authority safety property survives CRB's addition.
        import pytest as _pt
        with _pt.raises(RuntimeError, match="cannot be constructed directly"):
            event_stream.EventEmitter(source_module="crb")


class TestActionLogActionTypeSurface:
    def test_allowed_action_types_unchanged_by_crb(self):
        # CRB does NOT extend the cohort_action_log surface. CRB has
        # its own install_proposals durability; cohort_action_log
        # remains Drafter-and-future-cohort substrate.
        assert ALLOWED_ACTION_TYPES == frozenset({
            "create_draft", "update_draft", "abandon_draft",
            "emit_signal", "emit_receipt",
        })

    def test_action_log_status_constants(self):
        assert STATUS_PENDING == "pending"
        assert STATUS_PERFORMED == "performed"
        assert STATUS_FAILED == "failed"


class TestDrafterV2SurfacesUnchanged:
    def test_signal_types_pinned(self):
        assert SIGNAL_TYPES == frozenset({
            "drafter.signal.draft_ready",
            "drafter.signal.gap_detected",
            "drafter.signal.multi_intent_detected",
            "drafter.signal.idle_resurface",
            "drafter.signal.draft_paused",
            "drafter.signal.draft_abandoned",
        })

    def test_drafter_cohort_id(self):
        assert COHORT_ID == "drafter"

    def test_drafter_port_mark_committed_still_absent(self):
        """Drafter v2 AC #10 + #28 invariant survives CRB."""
        assert not hasattr(DrafterDraftPort, "mark_committed")


class TestDrafterV11SurfacesIntact:
    def test_feedback_received_in_receipt_types(self):
        assert "drafter.receipt.feedback_received" in RECEIPT_TYPES

    def test_crb_feedback_event_in_subscription(self):
        assert "crb.feedback.modify_request" in SUBSCRIBED_EVENT_TYPES


class TestDrafterV12SurfacesIntact:
    def test_candidate_intent_dataclass_present(self):
        # Single source of truth for the v1.2 candidate shape.
        assert CandidateIntent.__dataclass_fields__.keys() >= {
            "candidate_id", "summary", "confidence", "target_workflow_id",
        }


class TestCRBSurfaceLanded:
    def test_crb_event_types_pinned(self):
        assert CRB_EVENT_TYPES == frozenset({
            "routine.proposed",
            "routine.approved",
            "routine.modification.approved",
            "routine.declined",
            "crb.feedback.modify_request",
        })

    def test_crb_source_module_pinned(self):
        assert CRB_SOURCE_MODULE == "crb"

    def test_no_production_code_emits_crb_event_types_directly(self):
        """Codex final-review hardening (secondary): non-CRB production
        code must not call ``event_stream.emit`` with a CRB event
        type. The substrate enforces source_module via the
        EmitterRegistry and STS rejects approvals where
        envelope.source_module != 'crb', so a bypass would be
        rejected at the gate — but a static pin gives belt-and-braces
        and helps reviewers spot drift.

        The check walks production ``.py`` files under ``kernos/`` and
        asserts none outside the approved CRB seams contain a string
        literal CRB event type immediately following an
        ``event_stream.emit`` / ``emitter.emit`` call.
        """
        import pathlib
        import re

        approved_paths: tuple[str, ...] = (
            "kernos/kernel/crb/events.py",
            # principal_integration owns the typed receipt-ack emitter
            # and routes everything through Drafter port — listed
            # defensively even though it doesn't emit CRB types.
            "kernos/kernel/crb/principal_integration/",
        )
        crb_event_literals = (
            '"routine.proposed"', "'routine.proposed'",
            '"routine.approved"', "'routine.approved'",
            '"routine.modification.approved"',
            "'routine.modification.approved'",
            '"routine.declined"', "'routine.declined'",
            '"crb.feedback.modify_request"',
            "'crb.feedback.modify_request'",
        )
        emit_call_pattern = re.compile(
            r"\.emit\s*\([^)]*?(" + "|".join(
                re.escape(lit) for lit in crb_event_literals
            ) + r")", re.DOTALL,
        )
        repo_root = pathlib.Path(__file__).parent.parent
        offenders: list[str] = []
        for py in (repo_root / "kernos").rglob("*.py"):
            rel = py.relative_to(repo_root).as_posix()
            if any(rel.startswith(a) for a in approved_paths):
                continue
            text = py.read_text()
            if emit_call_pattern.search(text):
                offenders.append(rel)
        assert offenders == [], (
            f"production code outside CRB seams emits CRB event types "
            f"directly: {offenders}"
        )
