"""Event envelope + EmitterRegistry tests (STS C0).

Spec reference: SPEC-STS-v2 AC #23 — event envelope substrate-set with
EmitterRegistry uniqueness. The envelope's ``source_module`` is set by
the substrate at emit time from the registered emitter's identity, NOT
from caller-supplied payload. This is the trust boundary that makes
approval source authority structurally enforceable for STS.
"""
from __future__ import annotations

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.event_stream import (
    EmitterAlreadyRegistered,
    EmitterRegistry,
    EventEmitter,
    EventEnvelope,
    UNREGISTERED_SOURCE_MODULE,
)


@pytest.fixture
async def writer(tmp_path):
    """Fresh writer + registry per test."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    yield tmp_path
    await event_stream._reset_for_tests()


class TestEnvelopeShape:
    """The envelope is a substrate-set bundle distinct from payload."""

    async def test_event_exposes_envelope_property(self, writer):
        emitter = event_stream.emitter_registry().register("crb")
        await emitter.emit("inst_a", "routine.proposed", {"correlation_id": "c1"})
        await event_stream.flush_now()
        events = await event_stream.events_in_window(
            "inst_a",
            since=__import__("datetime").datetime.fromtimestamp(0, tz=__import__("datetime").timezone.utc),
            until=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert len(events) == 1
        env = events[0].envelope
        assert isinstance(env, EventEnvelope)
        assert env.source_module == "crb"
        assert env.event_id == events[0].event_id
        assert env.emitted_at == events[0].timestamp

    async def test_envelope_is_frozen(self, writer):
        env = EventEnvelope(source_module="crb", emitted_at="2026-04-29T00:00:00", event_id="e1")
        with pytest.raises((AttributeError, Exception)):
            env.source_module = "spoofed"  # type: ignore[misc]


class TestSubstrateSetSourceModule:
    """Envelope.source_module is set from the registered emitter's identity,
    not from caller-supplied payload."""

    async def test_registered_emitter_stamps_envelope(self, writer):
        emitter = event_stream.emitter_registry().register("crb")
        await emitter.emit("inst_a", "routine.approved", {"correlation_id": "c1"})
        await event_stream.flush_now()
        events = await event_stream.events_for_member("inst_a", member_id="any") or \
            await _fetch_all(writer)
        crb_events = [e for e in events if e.event_type == "routine.approved"]
        assert len(crb_events) == 1
        assert crb_events[0].envelope.source_module == "crb"

    async def test_payload_cannot_spoof_envelope_source(self, writer):
        """The classic spoof attempt: caller registers as 'foo' but tries to
        smuggle source_module='crb' into the payload. The envelope MUST
        reflect 'foo' regardless of payload contents."""
        emitter = event_stream.emitter_registry().register("foo")
        await emitter.emit(
            "inst_a", "routine.approved",
            {"source_module": "crb", "correlation_id": "c1"},
        )
        await event_stream.flush_now()
        events = await _fetch_all(writer)
        match = [e for e in events if e.event_type == "routine.approved"]
        assert len(match) == 1
        assert match[0].envelope.source_module == "foo"
        # Payload still carries the bogus claim — that's fine; STS reads
        # from the envelope, not the payload.
        assert match[0].payload.get("source_module") == "crb"

    async def test_legacy_emit_uses_unregistered_sentinel(self, writer):
        """The legacy module-level emit() does NOT register. Events get
        envelope.source_module == UNREGISTERED_SOURCE_MODULE so source
        gates fail closed."""
        await event_stream.emit("inst_a", "tool.called", {"foo": "bar"})
        await event_stream.flush_now()
        events = await _fetch_all(writer)
        assert len(events) == 1
        assert events[0].envelope.source_module == UNREGISTERED_SOURCE_MODULE

    async def test_legacy_emit_payload_source_module_is_ignored(self, writer):
        """A caller using the legacy emit() cannot smuggle authority via
        payload either."""
        await event_stream.emit(
            "inst_a", "tool.called", {"source_module": "crb"},
        )
        await event_stream.flush_now()
        events = await _fetch_all(writer)
        assert len(events) == 1
        assert events[0].envelope.source_module == UNREGISTERED_SOURCE_MODULE
        assert events[0].payload.get("source_module") == "crb"


class TestEmitterRegistryUniqueness:
    """Substrate enforces that only one emitter may claim a given source_module."""

    async def test_register_returns_emitter_with_frozen_source(self):
        await event_stream._reset_for_tests()
        emitter = event_stream.emitter_registry().register("crb")
        assert isinstance(emitter, EventEmitter)
        assert emitter.source_module == "crb"

    async def test_duplicate_registration_raises(self):
        await event_stream._reset_for_tests()
        event_stream.emitter_registry().register("crb")
        with pytest.raises(EmitterAlreadyRegistered):
            event_stream.emitter_registry().register("crb")

    async def test_distinct_modules_can_coexist(self):
        await event_stream._reset_for_tests()
        a = event_stream.emitter_registry().register("crb")
        b = event_stream.emitter_registry().register("drafter")
        assert a is not b
        assert a.source_module == "crb"
        assert b.source_module == "drafter"

    async def test_unregistered_sentinel_is_reserved(self):
        await event_stream._reset_for_tests()
        with pytest.raises(ValueError):
            event_stream.emitter_registry().register(UNREGISTERED_SOURCE_MODULE)

    async def test_empty_source_module_rejected(self):
        await event_stream._reset_for_tests()
        with pytest.raises(ValueError):
            event_stream.emitter_registry().register("")

    async def test_registry_lookup(self):
        await event_stream._reset_for_tests()
        emitter = event_stream.emitter_registry().register("crb")
        registry = event_stream.emitter_registry()
        assert registry.get("crb") is emitter
        assert registry.is_registered("crb") is True
        assert registry.get("missing") is None
        assert registry.is_registered("missing") is False


class TestSchemaPersistence:
    """The substrate persists source_module to a dedicated SQLite column;
    rehydration produces the same envelope. Lazy migration adds the
    column to legacy databases that predate STS."""

    async def test_emitted_event_round_trips_through_sqlite(self, writer):
        emitter = event_stream.emitter_registry().register("crb")
        await emitter.emit("inst_a", "routine.proposed", {"correlation_id": "c1"})
        await event_stream.flush_now()
        events = await _fetch_all(writer)
        assert len(events) == 1
        # The envelope on the rehydrated event reflects what was
        # persisted, not the in-memory dataclass.
        assert events[0].envelope.source_module == "crb"

    async def test_lazy_migration_on_legacy_database(self, tmp_path):
        """A pre-STS instance.db has no source_module column. On first
        start, the substrate adds the column; existing rows materialize
        with envelope.source_module == UNREGISTERED_SOURCE_MODULE."""
        await event_stream._reset_for_tests()
        db_path = tmp_path / "instance.db"
        # Build a legacy-shaped events table without the source_module column.
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                """
                CREATE TABLE events (
                    event_id        TEXT PRIMARY KEY,
                    instance_id     TEXT NOT NULL,
                    member_id       TEXT,
                    space_id        TEXT,
                    timestamp       TEXT NOT NULL,
                    event_type      TEXT NOT NULL,
                    payload         TEXT NOT NULL,
                    correlation_id  TEXT
                )
                """
            )
            await db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy_evt", "inst_a", None, None,
                    "2026-04-29T00:00:00+00:00", "tool.called", "{}", None,
                ),
            )
            await db.commit()

        await event_stream.start_writer(str(tmp_path))
        try:
            events = await _fetch_all(tmp_path)
            assert any(e.event_id == "legacy_evt" for e in events)
            legacy = next(e for e in events if e.event_id == "legacy_evt")
            assert legacy.envelope.source_module == UNREGISTERED_SOURCE_MODULE
        finally:
            await event_stream._reset_for_tests()


async def _fetch_all(data_dir):
    """Helper: pull every event out via a direct SELECT for isolation."""
    import datetime as dt
    return await event_stream.events_in_window(
        "inst_a",
        since=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
        until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
    )
