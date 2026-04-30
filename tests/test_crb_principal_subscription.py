"""Principal subscription tests (CRB C5, AC #26).

Pins:

* discover_subscription_path returns Path B by default (no existing
  durable mechanism in this codebase).
* Path B adoption: PrincipalSubscriptionAdapter constructs a
  DurableEventCursor scoped to (cohort_id="principal", instance_id).
* Path A: caller-supplied existing_ingest used; no cursor wired.
* Restart-recovery: cursor durability across re-construction
  (signal still in queue invariant).
* Subscription event types pinned to the 6 Drafter signal types.
"""
from __future__ import annotations

import datetime as dt

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)
from kernos.kernel.crb.principal_integration.subscription import (
    DiscoveredPath,
    PRINCIPAL_SUBSCRIBED_EVENT_TYPES,
    PrincipalSubscriptionAdapter,
    discover_subscription_path,
)


@pytest.fixture
async def cursor_store(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    store = CursorStore()
    await store.start(str(tmp_path))
    yield store
    await store.stop()
    await event_stream._reset_for_tests()


# ===========================================================================
# Path discovery
# ===========================================================================


class TestPathDiscovery:
    def test_default_returns_path_b(self):
        # No existing mechanism -> adopt cursor substrate.
        assert discover_subscription_path(
            has_existing_durable_mechanism=False,
        ) == DiscoveredPath.PATH_B_CURSOR_ADOPTED

    def test_existing_returns_path_a(self):
        assert discover_subscription_path(
            has_existing_durable_mechanism=True,
        ) == DiscoveredPath.PATH_A_EXISTING


# ===========================================================================
# Subscribed event types
# ===========================================================================


class TestSubscribedEventTypes:
    def test_six_drafter_signal_types_pinned(self):
        assert PRINCIPAL_SUBSCRIBED_EVENT_TYPES == frozenset({
            "drafter.signal.draft_ready",
            "drafter.signal.gap_detected",
            "drafter.signal.multi_intent_detected",
            "drafter.signal.idle_resurface",
            "drafter.signal.draft_paused",
            "drafter.signal.draft_abandoned",
        })


# ===========================================================================
# Path B — cursor adoption (default)
# ===========================================================================


class TestPathBCursorAdoption:
    async def test_path_b_constructs_cursor(self, cursor_store):
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        sub = await adapter.start(instance_id="inst_a")
        assert sub.path == DiscoveredPath.PATH_B_CURSOR_ADOPTED
        assert sub.cursor is not None
        assert sub.cursor.cohort_id == "principal"
        assert sub.cursor.instance_id == "inst_a"
        assert sub.ingest is None

    async def test_path_b_cursor_filters_to_drafter_signal_types(
        self, cursor_store,
    ):
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        sub = await adapter.start(instance_id="inst_a")
        assert sub.cursor.event_types == PRINCIPAL_SUBSCRIBED_EVENT_TYPES

    async def test_path_b_subscribes_to_drafter_signals(self, cursor_store):
        """Round-trip pin: emit a Drafter signal, verify the principal
        cursor delivers it; emit a non-Drafter event, verify it doesn't."""
        # Register a drafter emitter and emit two events.
        drafter = event_stream.emitter_registry().register("drafter")
        await drafter.emit(
            "inst_a", "drafter.signal.draft_ready",
            {"draft_id": "d-1", "descriptor_hash": "h" * 64},
        )
        await event_stream.emit(
            "inst_a", "tool.called", {"unrelated": True},
        )
        await event_stream.flush_now()
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        sub = await adapter.start(instance_id="inst_a")
        events = await sub.cursor.read_next_batch(max_events=10)
        # Only the Drafter signal delivered.
        assert len(events) == 1
        assert events[0].event_type == "drafter.signal.draft_ready"


# ===========================================================================
# Path A — existing mechanism
# ===========================================================================


class TestPathAExistingMechanism:
    async def test_path_a_uses_supplied_ingest(self, cursor_store):
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        ingested = []

        async def custom_ingest(event):
            ingested.append(event)

        sub = await adapter.start(
            instance_id="inst_a",
            has_existing_durable_mechanism=True,
            existing_ingest=custom_ingest,
        )
        assert sub.path == DiscoveredPath.PATH_A_EXISTING
        assert sub.cursor is None
        assert sub.ingest is custom_ingest

    async def test_path_a_without_ingest_callable_raises(self, cursor_store):
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        with pytest.raises(ValueError, match="existing_ingest"):
            await adapter.start(
                instance_id="inst_a",
                has_existing_durable_mechanism=True,
            )


# ===========================================================================
# Restart recovery (Path B)
# ===========================================================================


class TestRestartRecovery:
    async def test_path_b_cursor_persists_across_restart(self, cursor_store):
        """Engine startup wires Path B; cursor state durable across
        re-construction. Same property Drafter v2 already pins."""
        # First wire.
        adapter = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        sub_a = await adapter.start(instance_id="inst_a")
        # Emit a signal, read it, commit position.
        drafter = event_stream.emitter_registry().register("drafter")
        await drafter.emit(
            "inst_a", "drafter.signal.draft_ready",
            {"draft_id": "d-1"},
        )
        await event_stream.flush_now()
        events = await sub_a.cursor.read_next_batch(max_events=10)
        assert len(events) == 1
        await sub_a.cursor.commit_position(
            event_id=events[0].event_id,
            timestamp=events[0].timestamp,
        )
        # Re-wire (simulates engine restart).
        adapter_b = PrincipalSubscriptionAdapter(cursor_store=cursor_store)
        sub_b = await adapter_b.start(instance_id="inst_a")
        # No new signals emitted; cursor resumes past the last.
        new_events = await sub_b.cursor.read_next_batch(max_events=10)
        assert new_events == []
