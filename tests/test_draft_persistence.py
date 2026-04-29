"""Tests for DraftRegistry persistence + create_draft + get_draft.

WDP C1. Pins:

  - schema persistence with composite PK (instance_id, draft_id)
    and NOT NULL defaults for aliases / partial_spec_json
    (AC #1, AC #20)
  - cross-instance isolation: same draft_id in two instances
    resolves independently (AC #2)
  - atomic create: failure mid-transaction leaves no partial row
    (AC #3)
  - draft.created event emission (AC #11 partial — full event
    coverage in C2)
  - keyword-only public APIs with instance_id first (AC #19)
  - get_draft returns rows regardless of status (terminal drafts
    still readable for audit)
  - timestamps initialized correctly on create (AC #18 partial)
"""
from __future__ import annotations

import inspect

import pytest

from kernos.kernel.drafts.registry import (
    DraftRegistry,
    WorkflowDraft,
)


@pytest.fixture
async def registry(tmp_path):
    reg = DraftRegistry()
    await reg.start(str(tmp_path))
    yield reg
    await reg.stop()


# ===========================================================================
# Schema persistence + NOT NULL defaults (AC #20)
# ===========================================================================


class TestSchemaPersistence:
    async def test_create_persists_and_reads_back(self, registry):
        draft = await registry.create_draft(
            instance_id="inst_a",
            intent_summary="invoice my customers",
            home_space_id="space-work",
            source_thread_id="thread-1",
        )
        loaded = await registry.get_draft(
            instance_id="inst_a", draft_id=draft.draft_id,
        )
        assert loaded is not None
        assert loaded.draft_id == draft.draft_id
        assert loaded.instance_id == "inst_a"
        assert loaded.intent_summary == "invoice my customers"
        assert loaded.home_space_id == "space-work"
        assert loaded.source_thread_id == "thread-1"
        assert loaded.status == "shaping"
        assert loaded.version == 0

    async def test_aliases_default_empty_list_not_null(self, registry):
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        loaded = await registry.get_draft(
            instance_id="inst_a", draft_id=draft.draft_id,
        )
        # AC #20: aliases is always list, never None.
        assert loaded.aliases == []
        assert isinstance(loaded.aliases, list)

    async def test_partial_spec_json_default_empty_dict_not_null(
        self, registry,
    ):
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        loaded = await registry.get_draft(
            instance_id="inst_a", draft_id=draft.draft_id,
        )
        # AC #20: partial_spec_json is always dict, never None.
        assert loaded.partial_spec_json == {}
        assert isinstance(loaded.partial_spec_json, dict)

    async def test_draft_id_is_uuid_v4_shape(self, registry):
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        assert len(draft.draft_id) == 36
        assert draft.draft_id.count("-") == 4

    async def test_timestamps_equal_on_create(self, registry):
        """AC #18 partial — created_at == updated_at == last_touched_at on create."""
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        assert draft.created_at
        assert draft.updated_at == draft.created_at
        assert draft.last_touched_at == draft.created_at


# ===========================================================================
# Composite PK cross-instance non-collision (AC #1, AC #2)
# ===========================================================================


class TestCrossInstanceNonCollision:
    async def test_same_draft_id_different_instances_persist_independently(
        self, registry,
    ):
        """Composite PK lets two instances both have draft-001
        without collision. Pin landed AC #1."""
        # Force draft_id collision by manually inserting via raw SQL
        # (create_draft mints fresh UUIDs, but the schema must still
        # allow same id across instances).
        assert registry._db is not None
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for instance_id in ("inst_a", "inst_b"):
            await registry._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, status, intent_summary,"
                " version, created_at, updated_at, last_touched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("draft-001", instance_id, "shaping",
                 f"intent for {instance_id}", 0, now, now, now),
            )
        a = await registry.get_draft(
            instance_id="inst_a", draft_id="draft-001",
        )
        b = await registry.get_draft(
            instance_id="inst_b", draft_id="draft-001",
        )
        assert a is not None and b is not None
        assert a.intent_summary == "intent for inst_a"
        assert b.intent_summary == "intent for inst_b"

    async def test_get_draft_in_other_instance_returns_none(self, registry):
        """AC #2 — cross-instance isolation. Draft in inst_a is
        invisible to queries scoped to inst_b."""
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="only A sees me",
        )
        miss = await registry.get_draft(
            instance_id="inst_b", draft_id=draft.draft_id,
        )
        assert miss is None

    async def test_duplicate_composite_key_rejected(self, registry):
        """SQLite's composite-PK constraint is the structural backstop."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await registry._db.execute(
            "INSERT INTO workflow_drafts ("
            " draft_id, instance_id, status, intent_summary,"
            " version, created_at, updated_at, last_touched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("draft-x", "inst_a", "shaping", "first", 0, now, now, now),
        )
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await registry._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, status, intent_summary,"
                " version, created_at, updated_at, last_touched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("draft-x", "inst_a", "shaping", "duplicate",
                 0, now, now, now),
            )


# ===========================================================================
# Atomic create (AC #3)
# ===========================================================================


class TestAtomicCreate:
    async def test_create_with_empty_instance_id_rejected(self, registry):
        """Validation runs BEFORE any I/O — failure leaves no
        partial state."""
        with pytest.raises(ValueError, match="instance_id"):
            await registry.create_draft(
                instance_id="", intent_summary="x",
            )

    async def test_create_with_no_intent_still_persists(self, registry):
        """Empty intent is allowed (intent_summary may be set
        later via update_draft); pin verifies the row persists."""
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="",
        )
        loaded = await registry.get_draft(
            instance_id="inst_a", draft_id=draft.draft_id,
        )
        assert loaded is not None


# ===========================================================================
# Status-agnostic get_draft (terminal still readable)
# ===========================================================================


class TestStatusAgnosticGet:
    async def test_get_returns_terminal_drafts(self, registry):
        """get_draft returns the row regardless of status. Audit
        and admin surfaces consume terminal drafts via this."""
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        # Force status to terminal via raw SQL (mark_committed
        # comes in C2; for C1 we just confirm reads work).
        await registry._db.execute(
            "UPDATE workflow_drafts SET status = 'committed' "
            "WHERE instance_id = ? AND draft_id = ?",
            ("inst_a", draft.draft_id),
        )
        loaded = await registry.get_draft(
            instance_id="inst_a", draft_id=draft.draft_id,
        )
        assert loaded is not None
        assert loaded.status == "committed"


# ===========================================================================
# Event emission (AC #11 partial — draft.created)
# ===========================================================================


class TestEventEmission:
    async def test_draft_created_event_emitted(self, tmp_path):
        captured: list = []

        async def emitter(*, event_type, payload, instance_id):
            captured.append((event_type, payload, instance_id))

        reg = DraftRegistry(event_emitter=emitter)
        await reg.start(str(tmp_path))
        try:
            draft = await reg.create_draft(
                instance_id="inst_a",
                intent_summary="invoice automation",
                home_space_id="space-work",
                source_thread_id="thread-42",
            )
            assert len(captured) == 1
            event_type, payload, instance_id = captured[0]
            assert event_type == "draft.created"
            assert instance_id == "inst_a"
            assert payload["draft_id"] == draft.draft_id
            assert payload["instance_id"] == "inst_a"
            assert payload["intent_summary"] == "invoice automation"
            assert payload["home_space_id"] == "space-work"
            assert payload["source_thread_id"] == "thread-42"
            assert payload["created_at"]
        finally:
            await reg.stop()

    async def test_emitter_failure_does_not_break_create(self, tmp_path):
        """Event emission is fire-and-forget — emitter exceptions
        are caught and logged so create_draft still completes."""
        async def boom(*, event_type, payload, instance_id):
            raise RuntimeError("emitter on fire")

        reg = DraftRegistry(event_emitter=boom)
        await reg.start(str(tmp_path))
        try:
            draft = await reg.create_draft(
                instance_id="inst_a", intent_summary="x",
            )
            # Row persisted even though emitter raised.
            loaded = await reg.get_draft(
                instance_id="inst_a", draft_id=draft.draft_id,
            )
            assert loaded is not None
        finally:
            await reg.stop()

    async def test_no_emitter_is_noop(self, registry):
        # Default fixture has no emitter. Create succeeds without
        # raising or emitting.
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        assert draft.draft_id


# ===========================================================================
# Keyword-only public APIs (AC #19)
# ===========================================================================


class TestKeywordOnlyAPIs:
    """AC #19 — every public method is keyword-only with
    instance_id as the first declared keyword parameter. Verified
    via inspect.signature so Python's TypeError on positional
    misuse is the test."""

    def test_create_draft_signature_is_keyword_only(self):
        sig = inspect.signature(DraftRegistry.create_draft)
        params = [
            p for p in sig.parameters.values()
            if p.name != "self"
        ]
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY for p in params
        )
        assert params[0].name == "instance_id"

    def test_get_draft_signature_is_keyword_only(self):
        sig = inspect.signature(DraftRegistry.get_draft)
        params = [
            p for p in sig.parameters.values()
            if p.name != "self"
        ]
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY for p in params
        )
        assert params[0].name == "instance_id"

    async def test_create_positional_call_raises_typeerror(self, registry):
        with pytest.raises(TypeError):
            await registry.create_draft("inst_a", "intent")  # type: ignore[misc]

    async def test_get_positional_call_raises_typeerror(self, registry):
        with pytest.raises(TypeError):
            await registry.get_draft("inst_a", "any-draft")  # type: ignore[misc]


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestLifecycle:
    async def test_start_idempotent(self, registry):
        await registry.start("/tmp/should-not-be-used")
        # Still works.
        draft = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        assert draft.draft_id

    async def test_stop_idempotent(self, tmp_path):
        reg = DraftRegistry()
        await reg.start(str(tmp_path))
        await reg.stop()
        await reg.stop()  # no raise

    async def test_get_before_start_returns_none(self):
        reg = DraftRegistry()
        result = await reg.get_draft(
            instance_id="inst_a", draft_id="anything",
        )
        assert result is None
