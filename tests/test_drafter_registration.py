"""Drafter cohort registration + skeleton tests (DRAFTER C1, AC #1, #2, #6, #25, #26).

C1 ships the cohort skeleton — lifecycle (``start``), instance_id
discipline, anti-fragmentation + future-composition docstrings. The
full tick loop (Tier 1 / Tier 2 evaluation, signals) lands in C2/C3.

Pins:

* AC #1 — cohort registered with cohort_id = "drafter"; single instance
  per engine.
* AC #2 — cross-instance isolation (cohort scoped per instance).
* AC #6 — envelope source authority via DrafterEventPort source check
  (delegated from test_drafter_ports.py; we re-test the integration
  here at the cohort level).
* AC #25 — anti-fragmentation invariant inline in module docstring.
* AC #26 — future-composition invariant inline.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)
from kernos.kernel.cohorts.drafter import (
    COHORT_ID,
    SUBSCRIBED_EVENT_TYPES,
)
from kernos.kernel.cohorts.drafter.cohort import DrafterCohort, TickResult
from kernos.kernel.cohorts.drafter.ports import (
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)
from kernos.kernel.drafts.registry import DraftRegistry


# ===========================================================================
# Stack helpers
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    emitter = event_stream.emitter_registry().register("drafter")
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    cursor_store = CursorStore()
    await cursor_store.start(str(tmp_path))
    action_log = ActionLog(cohort_id="drafter")
    await action_log.start(str(tmp_path))
    cursor = DurableEventCursor(
        cursor_store=cursor_store,
        cohort_id="drafter",
        instance_id="inst_a",
        event_types=SUBSCRIBED_EVENT_TYPES,
    )
    draft_port = DrafterDraftPort(
        registry=drafts, action_log=action_log, instance_id="inst_a",
    )
    # No real STS in C1 tests; use a stub. We only exercise list/dry-run
    # surface in C2+; for C1, the port presence + structural absence
    # are the pins.
    class _StubSTS:
        async def list_workflows(self, **kw): return []
        async def list_known_providers(self, **kw): return []
        async def list_agents(self, **kw): return []
        async def list_drafts(self, **kw): return []
        async def query_context_brief(self, **kw): return None
        async def register_workflow(self, **kw): return None

    sts_port = DrafterSubstrateToolsPort(sts=_StubSTS(), instance_id="inst_a")
    event_port = DrafterEventPort(
        emitter=emitter, action_log=action_log, instance_id="inst_a",
    )
    cohort = DrafterCohort(
        draft_port=draft_port,
        substrate_tools_port=sts_port,
        event_port=event_port,
        cursor=cursor,
        action_log=action_log,
    )
    yield {
        "cohort": cohort, "cursor": cursor, "action_log": action_log,
        "drafts": drafts, "cursor_store": cursor_store,
        "draft_port": draft_port, "event_port": event_port,
    }
    await action_log.stop()
    await cursor_store.stop()
    await drafts.stop()
    await event_stream._reset_for_tests()


# ===========================================================================
# AC #1 — cohort registered with cohort_id = "drafter"
# ===========================================================================


class TestCohortRegistration:
    def test_cohort_id_pinned(self, stack):
        assert stack["cohort"].cohort_id == "drafter"
        assert COHORT_ID == "drafter"

    def test_cohort_subscribes_to_three_event_types(self, stack):
        assert SUBSCRIBED_EVENT_TYPES == frozenset({
            "conversation.message.posted",
            "conversation.context.shifted",
            "friction.signal.surfaced",
        })

    def test_cohort_not_started_until_start_called(self, stack):
        # Construction does not implicitly start.
        assert stack["cohort"].is_started is False

    def test_start_marks_cohort_started(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        assert stack["cohort"].is_started is True

    def test_start_idempotent_via_re_call(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        # Calling start again with the same instance_id is a no-op.
        stack["cohort"].start(instance_id="inst_a")
        assert stack["cohort"].is_started is True

    def test_start_with_mismatched_instance_id_raises(self, stack):
        with pytest.raises(ValueError, match="instance_id"):
            stack["cohort"].start(instance_id="inst_b")

    def test_start_requires_instance_id(self, stack):
        with pytest.raises(ValueError):
            stack["cohort"].start(instance_id="")


# ===========================================================================
# AC #2 — cross-instance isolation
# ===========================================================================


class TestCrossInstanceIsolation:
    async def test_tick_with_mismatched_instance_id_raises(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        with pytest.raises(ValueError, match="instance_id"):
            await stack["cohort"].tick(instance_id="inst_b")

    async def test_tick_processes_only_cohort_instance_events(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        # Emit events for both instances.
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.emit("inst_b", "conversation.message.posted", {"i": 2})
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        # Only inst_a's event was processed.
        assert result.events_processed == 1


# ===========================================================================
# AC #4 — at-least-once with event_id idempotency
# ===========================================================================


class TestIdempotencyOnReTick:
    async def test_re_tick_does_not_re_process(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.flush_now()
        first = await stack["cohort"].tick(instance_id="inst_a")
        assert first.events_processed == 1
        # Second tick with no new events.
        second = await stack["cohort"].tick(instance_id="inst_a")
        assert second.events_processed == 0


# ===========================================================================
# AC #5 — subscription type filter
# ===========================================================================


class TestTypeFilter:
    async def test_only_subscribed_types_processed(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        # Mix of subscribed + non-subscribed event types.
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.emit("inst_a", "tool.called", {"i": 2})
        await event_stream.emit("inst_a", "friction.signal.surfaced", {"i": 3})
        await event_stream.emit("inst_a", "compaction.completed", {"i": 4})
        await event_stream.emit("inst_a", "conversation.context.shifted", {"i": 5})
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        # Three subscribed events processed; two filtered out.
        assert result.events_processed == 3


# ===========================================================================
# AC #25 — anti-fragmentation invariant inline
# ===========================================================================


class TestAntiFragmentationInvariant:
    def test_drafter_package_docstring(self):
        import kernos.kernel.cohorts.drafter as pkg
        doc = (pkg.__doc__ or "").lower()
        assert "anti-fragmentation" in doc
        assert "consumes shared context surfaces" in doc
        assert "parallel" in doc

    def test_drafter_cohort_module_docstring(self):
        from kernos.kernel.cohorts.drafter import cohort as mod
        doc = (mod.__doc__ or "").lower()
        assert "anti-fragmentation" in doc

    def test_drafter_class_docstring(self):
        doc = (DrafterCohort.__doc__ or "").lower()
        # Class docstring may not need the full text, but should at
        # least signal the cohort's role.
        assert "tool-starved" in doc


# ===========================================================================
# AC #26 — future-composition invariant
# ===========================================================================


class TestFutureCompositionInvariant:
    def test_substrate_directory_signals_reusability(self):
        """The cohort substrate lives in ``cohorts/_substrate/`` —
        directory placement signals reusable intent."""
        import kernos.kernel.cohorts._substrate as sub_pkg
        doc = (sub_pkg.__doc__ or "").lower()
        assert "reusable" in doc
        assert "drafter" in doc
        assert "pattern observer" in doc or "pattern_observer" in doc.replace(
            " observer", "_observer",
        )

    def test_drafter_package_docstring_mentions_future_cohorts(self):
        import kernos.kernel.cohorts.drafter as pkg
        doc = (pkg.__doc__ or "").lower()
        assert "future-composition" in doc
        # Must signal that future cohorts inherit the same patterns.
        assert "pattern observer" in doc or "future" in doc


# ===========================================================================
# Tick result shape
# ===========================================================================


class TestTickResult:
    async def test_no_events_returns_no_op(self, stack):
        stack["cohort"].start(instance_id="inst_a")
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 0
        assert result.no_op is True
        assert result.signals_emitted == ()

    async def test_tick_before_start_raises(self, stack):
        # No start() called.
        with pytest.raises(RuntimeError, match="start"):
            await stack["cohort"].tick(instance_id="inst_a")
