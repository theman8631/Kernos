"""Tests for state machine + update_draft + mark_committed +
abandon_draft + envelope validation + optimistic concurrency.

WDP C2. Pins AC #4-15, #18, #19. ~28 tests.
"""
from __future__ import annotations

import inspect

import pytest

from kernos.kernel.drafts.errors import (
    DraftAliasCollision,
    DraftConcurrentModification,
    DraftEnvelopeInvalid,
    DraftNotFound,
    DraftTerminal,
    InvalidDraftTransition,
    ReadyStateMutationRequiresDemotion,
    WorkflowReferenceMissing,
)
from kernos.kernel.drafts.registry import (
    DraftRegistry,
    WorkflowDraft,
)


@pytest.fixture
async def registry(tmp_path):
    captured_events: list = []

    async def emitter(*, event_type, payload, instance_id):
        captured_events.append((event_type, payload, instance_id))

    reg = DraftRegistry(event_emitter=emitter)
    await reg.start(str(tmp_path))
    reg._test_events = captured_events  # type: ignore[attr-defined]
    yield reg
    await reg.stop()


def _events_of_type(reg, event_type: str) -> list:
    return [
        (et, p, iid) for (et, p, iid) in reg._test_events
        if et == event_type
    ]


# ===========================================================================
# State machine (AC #4)
# ===========================================================================


class TestStateMachine:
    async def test_shaping_to_blocked_allowed(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        assert out.status == "blocked"
        assert out.version == 1

    async def test_shaping_to_ready_allowed(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        assert out.status == "ready"

    async def test_blocked_to_shaping_allowed(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version, status="shaping",
        )
        assert out.status == "shaping"

    async def test_blocked_to_ready_direct_rejected(self, registry):
        """Spec: must go blocked → shaping → ready."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        with pytest.raises(InvalidDraftTransition):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version, status="ready",
            )

    async def test_ready_to_shaping_allowed(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version, status="shaping",
        )
        assert out.status == "shaping"

    async def test_unknown_status_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition, match="unknown"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="weird",
            )


# ===========================================================================
# Terminal-state guard (AC #5) + bypass-prevention (AC #6)
# ===========================================================================


class TestTerminalGuards:
    async def test_committed_blocks_update(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        await registry.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-1",
        )
        with pytest.raises(DraftTerminal):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=10, status="shaping",
            )

    async def test_abandoned_blocks_mutations(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
        )
        with pytest.raises(DraftTerminal):
            await registry.abandon_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
            )
        with pytest.raises(DraftTerminal):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version, status="shaping",
            )

    async def test_committed_via_update_rejected(self, registry):
        """AC #6: status='committed' is reachable only via
        mark_committed."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition,
                           match="mark_committed"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="committed",
            )

    async def test_abandoned_via_update_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition,
                           match="abandon_draft"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="abandoned",
            )


# ===========================================================================
# Optimistic concurrency (AC #7)
# ===========================================================================


class TestOptimisticConcurrency:
    async def test_stale_version_raises(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        # Caller A updates.
        await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        # Caller B with stale version=0.
        with pytest.raises(DraftConcurrentModification):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="shaping",
            )

    async def test_version_increments_on_each_mutation(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, intent_summary="updated",
        )
        assert out.version == 1
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=1, intent_summary="updated again",
        )
        assert out.version == 2


# ===========================================================================
# Envelope validation (AC #8) + secret non-echo (AC #9)
# ===========================================================================


class TestEnvelopeValidation:
    async def test_non_object_payload_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid, match="JSON object"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json=["not", "a", "dict"],
            )

    async def test_oversize_payload_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        big = {"k": "x" * (70 * 1024)}
        with pytest.raises(DraftEnvelopeInvalid, match="byte limit"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, partial_spec_json=big,
            )

    async def test_executable_blob_key_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid, match="executable"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"eval": "anything"},
            )

    async def test_secret_keyword_in_key_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid, match="secret-shaped key"):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"api_key": "value"},
            )

    async def test_valid_payload_accepted(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
            partial_spec_json={"trigger": "morning", "channel": "email"},
        )
        assert out.partial_spec_json == {
            "trigger": "morning", "channel": "email",
        }


class TestSecretNonEcho:
    """AC #9: error messages NEVER echo matched secret values."""

    async def test_secret_value_pattern_does_not_leak(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        # A JWT-shaped Bearer token.
        secret_value = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        with pytest.raises(DraftEnvelopeInvalid) as exc_info:
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"some_field": secret_value},
            )
        msg = str(exc_info.value)
        assert "some_field" in msg
        # The secret VALUE must NOT appear in the error.
        assert secret_value not in msg
        assert "eyJ" not in msg

    async def test_aws_key_pattern_does_not_leak(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        secret_value = "AKIAIOSFODNN7EXAMPLE"
        with pytest.raises(DraftEnvelopeInvalid) as exc_info:
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"creds": secret_value},
            )
        msg = str(exc_info.value)
        assert "creds" in msg
        assert secret_value not in msg


# ===========================================================================
# home_space_id event (AC #10) + full event coverage (AC #11)
# ===========================================================================


class TestEventEmissionFull:
    async def test_home_space_changed_event(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
            home_space_id="space-old",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, home_space_id="space-new",
        )
        events = _events_of_type(registry, "draft.home_space_changed")
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["old_home_space_id"] == "space-old"
        assert payload["new_home_space_id"] == "space-new"
        assert payload["draft_id"] == d.draft_id

    async def test_status_changed_event(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        events = _events_of_type(registry, "draft.status_changed")
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["old_status"] == "shaping"
        assert payload["new_status"] == "blocked"

    async def test_mark_committed_emits_both_events(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        # Clear prior events for cleaner assertion.
        registry._test_events.clear()
        await registry.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-target",
        )
        types = [e[0] for e in registry._test_events]
        assert "draft.status_changed" in types
        assert "draft.committed" in types
        committed_events = _events_of_type(registry, "draft.committed")
        _, payload, _ = committed_events[0]
        assert payload["committed_workflow_id"] == "wf-target"

    async def test_abandon_emits_both_events(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        registry._test_events.clear()
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
        )
        types = [e[0] for e in registry._test_events]
        assert "draft.status_changed" in types
        assert "draft.abandoned" in types
        abandoned_events = _events_of_type(registry, "draft.abandoned")
        _, payload, _ = abandoned_events[0]
        assert payload["prior_status"] == "shaping"


# ===========================================================================
# mark_committed soft-reference runtime check (AC #12) + status guard (AC #13)
# ===========================================================================


class TestMarkCommittedRuntimeCheck:
    async def test_workflow_registry_miss_raises(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )

        class FakeRegistry:
            async def exists(self, workflow_id, instance_id):
                return False

        with pytest.raises(WorkflowReferenceMissing):
            await registry.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                committed_workflow_id="missing-wf",
                workflow_registry=FakeRegistry(),
            )

    async def test_workflow_registry_hit_succeeds(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )

        class FakeRegistry:
            async def exists(self, workflow_id, instance_id):
                return True

        committed = await registry.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-real",
            workflow_registry=FakeRegistry(),
        )
        assert committed.status == "committed"
        assert committed.committed_workflow_id == "wf-real"

    async def test_no_registry_passes_through(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        committed = await registry.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-test",
        )
        assert committed.status == "committed"

    async def test_mark_committed_from_shaping_rejected(self, registry):
        """AC #13: mark_committed requires status='ready'."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition, match="ready"):
            await registry.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, committed_workflow_id="wf",
            )

    async def test_mark_committed_from_blocked_rejected(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        with pytest.raises(InvalidDraftTransition, match="ready"):
            await registry.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                committed_workflow_id="wf",
            )


# ===========================================================================
# Ready-state demotion (AC #14)
# ===========================================================================


class TestReadyStateDemotion:
    async def test_substantive_edit_on_ready_without_demotion_rejected(
        self, registry,
    ):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        with pytest.raises(ReadyStateMutationRequiresDemotion):
            await registry.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                partial_spec_json={"new": "shape"},
            )

    async def test_substantive_edit_with_explicit_demotion_succeeds(
        self, registry,
    ):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            status="shaping",
            partial_spec_json={"new": "shape"},
        )
        assert out.status == "shaping"
        assert out.partial_spec_json == {"new": "shape"}

    async def test_home_space_change_on_ready_without_demotion_succeeds(
        self, registry,
    ):
        """Non-substantive mutation: home_space_id only. No
        demotion required."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
            home_space_id="old-space",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            home_space_id="new-space",
        )
        assert out.status == "ready"  # stayed ready
        assert out.home_space_id == "new-space"


# ===========================================================================
# Alias collision (AC #15)
# ===========================================================================


class TestAliasCollision:
    async def test_collision_raises_typed_error(self, registry):
        a = await registry.create_draft(
            instance_id="inst_a", intent_summary="invoice",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["inv-routine"],
        )
        b = await registry.create_draft(
            instance_id="inst_a", intent_summary="something",
        )
        with pytest.raises(DraftAliasCollision):
            await registry.update_draft(
                instance_id="inst_a", draft_id=b.draft_id,
                expected_version=0, aliases=["inv-routine"],
            )

    async def test_collision_distinct_from_envelope_invalid(self, registry):
        """AC #15 — DraftAliasCollision is its own type, NOT
        DraftEnvelopeInvalid."""
        a = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["taken"],
        )
        b = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
        )
        with pytest.raises(DraftAliasCollision) as exc_info:
            await registry.update_draft(
                instance_id="inst_a", draft_id=b.draft_id,
                expected_version=0, aliases=["taken"],
            )
        assert not isinstance(exc_info.value, DraftEnvelopeInvalid)

    async def test_collision_case_insensitive(self, registry):
        a = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["Mixed-Case"],
        )
        b = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
        )
        with pytest.raises(DraftAliasCollision):
            await registry.update_draft(
                instance_id="inst_a", draft_id=b.draft_id,
                expected_version=0, aliases=["mixed-case"],
            )

    async def test_alias_collision_scoped_to_instance(self, registry):
        a = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["shared"],
        )
        b = await registry.create_draft(
            instance_id="inst_b", intent_summary="x",
        )
        # Same alias in different instance — fine.
        await registry.update_draft(
            instance_id="inst_b", draft_id=b.draft_id,
            expected_version=0, aliases=["shared"],
        )

    async def test_collision_skips_terminal_drafts(self, registry):
        """Aliases of abandoned drafts can be reclaimed."""
        a = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["reclaim"],
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=1,
        )
        b = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
        )
        # Should succeed — abandoned draft's aliases don't block.
        await registry.update_draft(
            instance_id="inst_a", draft_id=b.draft_id,
            expected_version=0, aliases=["reclaim"],
        )


# ===========================================================================
# last_touched_at independence (AC #18)
# ===========================================================================


class TestTouchedAtIndependence:
    async def test_status_only_update_advances_touched(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        before = d.last_touched_at
        # Sleep briefly then mutate so timestamps differ.
        import asyncio
        await asyncio.sleep(0.01)
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        assert out.last_touched_at > before

    async def test_field_only_update_advances_touched(self, registry):
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        before = d.last_touched_at
        import asyncio
        await asyncio.sleep(0.01)
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, intent_summary="updated",
        )
        assert out.last_touched_at > before


# ===========================================================================
# Keyword-only on remaining mutations (AC #19)
# ===========================================================================


class TestKeywordOnlyRemainingAPIs:
    def test_update_draft_kw_only(self):
        sig = inspect.signature(DraftRegistry.update_draft)
        params = [
            p for p in sig.parameters.values() if p.name != "self"
        ]
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY for p in params
        )
        assert params[0].name == "instance_id"

    def test_mark_committed_kw_only(self):
        sig = inspect.signature(DraftRegistry.mark_committed)
        params = [
            p for p in sig.parameters.values() if p.name != "self"
        ]
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY for p in params
        )
        assert params[0].name == "instance_id"

    def test_abandon_draft_kw_only(self):
        sig = inspect.signature(DraftRegistry.abandon_draft)
        params = [
            p for p in sig.parameters.values() if p.name != "self"
        ]
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY for p in params
        )
        assert params[0].name == "instance_id"


# ===========================================================================
# DraftNotFound on missing rows
# ===========================================================================


class TestNotFound:
    async def test_update_unknown_raises_notfound(self, registry):
        with pytest.raises(DraftNotFound):
            await registry.update_draft(
                instance_id="inst_a", draft_id="nonexistent",
                expected_version=0, status="blocked",
            )

    async def test_mark_committed_unknown_raises_notfound(self, registry):
        with pytest.raises(DraftNotFound):
            await registry.mark_committed(
                instance_id="inst_a", draft_id="nope",
                expected_version=0, committed_workflow_id="wf",
            )

    async def test_abandon_unknown_raises_notfound(self, registry):
        with pytest.raises(DraftNotFound):
            await registry.abandon_draft(
                instance_id="inst_a", draft_id="nope",
                expected_version=0,
            )
