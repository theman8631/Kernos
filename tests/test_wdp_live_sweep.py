"""WDP live sweep — 16 scenarios from the spec.

WDP C3. Each scenario mirrors the runbook at
data/diagnostics/live-tests/WDP-live-test.md and exercises the
substrate end-to-end with local providers (in-memory event
emitter, mock workflow_registry). No real WLP wiring during sweep
per WLP-GS standing convention.

Scenario index:
  1. Create-and-update happy path
  2. Status transitions valid (shaping → blocked → shaping → ready
     → committed)
  3. Status transitions invalid (forbidden direct paths)
  4. Terminal-state guards
  5. Optimistic concurrency conflict
  6. Envelope validation rejections
  7. Envelope error secret-non-echo
  8. home_space_id event emission
  9. mark_committed runtime check (hit / miss / omitted)
 10. mark_committed status guard
 11. abandon from each non-terminal state
 12. Cross-instance isolation
 13. Ready-state demotion required for substantive edits
 14. Alias collision typed error
 15. Cleanup safety
 16. Keyword-only API enforcement
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from kernos.kernel.drafts.errors import (
    DraftAliasCollision,
    DraftConcurrentModification,
    DraftEnvelopeInvalid,
    DraftTerminal,
    InvalidDraftTransition,
    ReadyStateMutationRequiresDemotion,
    WorkflowReferenceMissing,
)
from kernos.kernel.drafts.registry import DraftRegistry


@pytest.fixture
async def sweep_stack(tmp_path):
    captured: list = []

    async def emitter(*, event_type, payload, instance_id):
        captured.append((event_type, payload, instance_id))

    reg = DraftRegistry(event_emitter=emitter)
    await reg.start(str(tmp_path))
    yield reg, captured
    await reg.stop()


# ---------------------------------------------------------------------------


class TestScenario1CreateAndUpdate:
    async def test_happy_path(self, sweep_stack):
        reg, events = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a",
            intent_summary="invoice my customers",
        )
        # draft.created emitted
        assert any(e[0] == "draft.created" for e in events)
        first_touched = d.last_touched_at
        await asyncio.sleep(0.01)
        # Update display_name + aliases incrementally.
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, display_name="Invoice Routine",
        )
        assert out.display_name == "Invoice Routine"
        assert out.last_touched_at > first_touched
        await asyncio.sleep(0.01)
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=1, aliases=["invoice", "billing"],
        )
        assert out.aliases == ["invoice", "billing"]
        assert out.version == 2


class TestScenario2StatusTransitionsValid:
    async def test_full_path_to_committed(self, sweep_stack):
        reg, events = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        # shaping → blocked
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        # blocked → shaping
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version, status="shaping",
        )
        # shaping → ready
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version, status="ready",
        )
        # ready → committed via mark_committed
        await reg.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-final",
        )
        # status_changed events fired on every status mutation;
        # draft.committed fires at the end.
        types = [e[0] for e in events]
        status_changes = [e for e in events if e[0] == "draft.status_changed"]
        assert len(status_changes) == 4  # 3 update + 1 mark_committed
        assert "draft.committed" in types


class TestScenario3StatusTransitionsInvalid:
    async def test_shaping_to_committed_direct_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="committed",
            )

    async def test_blocked_to_ready_direct_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        with pytest.raises(InvalidDraftTransition):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version, status="ready",
            )


class TestScenario4TerminalGuards:
    async def test_committed_blocks_all_mutations(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        await reg.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-x",
        )
        # All three mutation methods raise DraftTerminal.
        with pytest.raises(DraftTerminal):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=99, status="shaping",
            )
        with pytest.raises(DraftTerminal):
            await reg.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=99, committed_workflow_id="wf-y",
            )
        with pytest.raises(DraftTerminal):
            await reg.abandon_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=99,
            )
        # But reads still succeed.
        loaded = await reg.get_draft(
            instance_id="inst_a", draft_id=d.draft_id,
        )
        assert loaded is not None and loaded.status == "committed"


class TestScenario5ConcurrencyConflict:
    async def test_stale_version_raises(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        with pytest.raises(DraftConcurrentModification):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, status="shaping",
            )


class TestScenario6EnvelopeValidation:
    async def test_non_object_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, partial_spec_json="just a string",
            )

    async def test_oversize_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"k": "x" * (70 * 1024)},
            )

    async def test_executable_blob_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"eval": "print('hi')"},
            )

    async def test_secret_keyword_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(DraftEnvelopeInvalid):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"private_key": "..."},
            )

    async def test_valid_payload_round_trips(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
            partial_spec_json={
                "trigger": {"event_type": "time.tick"},
                "actions": [{"verb": "notify_user"}],
            },
        )
        assert out.partial_spec_json["trigger"]["event_type"] == "time.tick"


class TestScenario7SecretNonEcho:
    async def test_pem_key_value_does_not_leak(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        secret = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAabcdef123456789secretdata\n"
            "-----END RSA PRIVATE KEY-----"
        )
        with pytest.raises(DraftEnvelopeInvalid) as exc_info:
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0,
                partial_spec_json={"deploy_key": secret},
            )
        msg = str(exc_info.value)
        assert "deploy_key" in msg
        # The secret VALUE must NOT appear in the error.
        assert "MIIEowIBAAK" not in msg
        assert "BEGIN RSA" not in msg
        assert "END RSA" not in msg


class TestScenario8HomeSpaceEvent:
    async def test_home_space_change_emits_event(self, sweep_stack):
        reg, events = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
            home_space_id="space-old",
        )
        await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, home_space_id="space-new",
        )
        space_events = [
            e for e in events if e[0] == "draft.home_space_changed"
        ]
        assert len(space_events) == 1
        _, payload, _ = space_events[0]
        assert payload["old_home_space_id"] == "space-old"
        assert payload["new_home_space_id"] == "space-new"


class TestScenario9MarkCommittedRuntimeCheck:
    async def test_registry_miss_raises(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )

        class Miss:
            async def exists(self, wf_id, instance_id):
                return False

        with pytest.raises(WorkflowReferenceMissing):
            await reg.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                committed_workflow_id="missing",
                workflow_registry=Miss(),
            )

    async def test_registry_hit_succeeds(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )

        class Hit:
            async def exists(self, wf_id, instance_id):
                return True

        out = await reg.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="real-wf",
            workflow_registry=Hit(),
        )
        assert out.status == "committed"

    async def test_no_registry_passes_through(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await reg.mark_committed(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            committed_workflow_id="any-wf",
        )
        assert out.status == "committed"


class TestScenario10MarkCommittedStatusGuard:
    async def test_from_shaping_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        with pytest.raises(InvalidDraftTransition):
            await reg.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=0, committed_workflow_id="wf",
            )

    async def test_from_blocked_rejected(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        with pytest.raises(InvalidDraftTransition):
            await reg.mark_committed(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                committed_workflow_id="wf",
            )


class TestScenario11AbandonFromEachState:
    async def test_from_shaping(self, sweep_stack):
        reg, events = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
        )
        assert out.status == "abandoned"
        types = [e[0] for e in events]
        assert "draft.status_changed" in types
        assert "draft.abandoned" in types

    async def test_from_blocked(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="blocked",
        )
        out = await reg.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
        )
        assert out.status == "abandoned"

    async def test_from_ready(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await reg.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
        )
        assert out.status == "abandoned"


class TestScenario12CrossInstanceIsolation:
    async def test_all_six_methods_scoped_per_instance(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        # get_draft
        miss = await reg.get_draft(
            instance_id="inst_b", draft_id=d.draft_id,
        )
        assert miss is None
        # list_drafts
        b_list = await reg.list_drafts(instance_id="inst_b")
        assert b_list == []
        # update_draft
        from kernos.kernel.drafts.errors import DraftNotFound
        with pytest.raises(DraftNotFound):
            await reg.update_draft(
                instance_id="inst_b", draft_id=d.draft_id,
                expected_version=0, status="blocked",
            )
        # mark_committed
        with pytest.raises(DraftNotFound):
            await reg.mark_committed(
                instance_id="inst_b", draft_id=d.draft_id,
                expected_version=0, committed_workflow_id="wf",
            )
        # abandon_draft
        with pytest.raises(DraftNotFound):
            await reg.abandon_draft(
                instance_id="inst_b", draft_id=d.draft_id,
                expected_version=0,
            )
        # cleanup_abandoned_older_than (returns 0; scoped to b's
        # rows; doesn't touch a's data)
        deleted = await reg.cleanup_abandoned_older_than(
            instance_id="inst_b", days=0,
        )
        assert deleted == 0


class TestScenario13ReadyDemotion:
    async def test_substantive_edit_without_demotion_raises(
        self, sweep_stack,
    ):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        with pytest.raises(ReadyStateMutationRequiresDemotion):
            await reg.update_draft(
                instance_id="inst_a", draft_id=d.draft_id,
                expected_version=out.version,
                partial_spec_json={"new": "shape"},
            )

    async def test_with_demotion_succeeds(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version,
            status="shaping",
            partial_spec_json={"new": "shape"},
        )
        assert out.status == "shaping"

    async def test_non_substantive_skips_demotion(self, sweep_stack):
        reg, _ = sweep_stack
        d = await reg.create_draft(
            instance_id="inst_a", intent_summary="x",
            home_space_id="old",
        )
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0, status="ready",
        )
        # home_space_id-only on ready: no demotion needed.
        out = await reg.update_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=out.version, home_space_id="new",
        )
        assert out.status == "ready"


class TestScenario14AliasCollision:
    async def test_collision_distinct_typed_error(self, sweep_stack):
        reg, _ = sweep_stack
        a = await reg.create_draft(
            instance_id="inst_a", intent_summary="invoice",
        )
        await reg.update_draft(
            instance_id="inst_a", draft_id=a.draft_id,
            expected_version=0, aliases=["inv-routine"],
        )
        b = await reg.create_draft(
            instance_id="inst_a", intent_summary="other",
        )
        with pytest.raises(DraftAliasCollision) as exc_info:
            await reg.update_draft(
                instance_id="inst_a", draft_id=b.draft_id,
                expected_version=0, aliases=["inv-routine"],
            )
        # Distinct from envelope-invalid.
        assert not isinstance(exc_info.value, DraftEnvelopeInvalid)


class TestScenario15CleanupSafety:
    async def test_only_old_abandoned_deleted(self, sweep_stack):
        reg, _ = sweep_stack
        old_iso = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        for state, draft_id in [
            ("shaping", "old-shaping"),
            ("ready", "old-ready"),
            ("committed", "old-committed"),
            ("abandoned", "old-abandoned"),
        ]:
            await reg._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, status, intent_summary,"
                " version, created_at, updated_at, last_touched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, "inst_a", state, "x",
                 0, old_iso, old_iso, old_iso),
            )
        deleted = await reg.cleanup_abandoned_older_than(
            instance_id="inst_a", days=7,
        )
        assert deleted == 1


class TestScenario16KeywordOnlyEnforcement:
    """Every public method on DraftRegistry rejects positional
    instance_id."""

    METHODS = [
        "create_draft", "get_draft", "update_draft",
        "mark_committed", "abandon_draft", "list_drafts",
        "cleanup_abandoned_older_than",
    ]

    def test_all_methods_have_kw_only_signatures(self):
        for method_name in self.METHODS:
            method = getattr(DraftRegistry, method_name)
            sig = inspect.signature(method)
            params = [
                p for p in sig.parameters.values() if p.name != "self"
            ]
            for p in params:
                assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                    f"{method_name} parameter {p.name!r} is not "
                    f"keyword-only"
                )
            assert params[0].name == "instance_id", (
                f"{method_name} first parameter is not instance_id"
            )

    async def test_positional_calls_raise_typeerror(self, sweep_stack):
        reg, _ = sweep_stack
        with pytest.raises(TypeError):
            await reg.create_draft("inst_a", "intent")  # type: ignore[misc]
        with pytest.raises(TypeError):
            await reg.get_draft("inst_a", "draft-x")  # type: ignore[misc]
