"""Tests for first-class Preference state (SPEC-6A-1)."""
import pytest

from kernos.kernel.state import Preference, generate_preference_id
from kernos.kernel.state_json import JsonStateStore
from kernos.utils import utc_now


@pytest.fixture
def store(tmp_path):
    return JsonStateStore(str(tmp_path))


def _make_pref(**kwargs) -> Preference:
    defaults = dict(
        id=generate_preference_id(),
        tenant_id="sms:+15555550100",
        intent="Notify me 10 minutes before appointments",
        category="notification",
        subject="calendar_events",
        action="notify",
        parameters={"lead_time_minutes": 10},
        scope="global",
        status="active",
        created_at=utc_now(),
    )
    defaults.update(kwargs)
    return Preference(**defaults)


# ---------------------------------------------------------------------------
# Creation and retrieval
# ---------------------------------------------------------------------------


async def test_add_and_get_preference(store):
    pref = _make_pref()
    await store.add_preference(pref)

    loaded = await store.get_preference(pref.tenant_id, pref.id)
    assert loaded is not None
    assert loaded.id == pref.id
    assert loaded.intent == pref.intent
    assert loaded.category == "notification"
    assert loaded.subject == "calendar_events"
    assert loaded.action == "notify"
    assert loaded.parameters == {"lead_time_minutes": 10}
    assert loaded.status == "active"


async def test_get_nonexistent_preference(store):
    result = await store.get_preference("sms:+15555550100", "pref_nonexistent")
    assert result is None


async def test_query_active_preferences(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(tenant_id=t, subject="calendar_events"))
    await store.add_preference(_make_pref(tenant_id=t, subject="email"))
    await store.add_preference(_make_pref(tenant_id=t, subject="responses", status="revoked"))

    active = await store.query_preferences(t, active_only=True)
    assert len(active) == 2

    all_prefs = await store.query_preferences(t, active_only=False)
    assert len(all_prefs) == 3


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


async def test_supersession(store):
    t = "sms:+15555550100"
    old_pref = _make_pref(tenant_id=t, parameters={"lead_time_minutes": 10})
    await store.add_preference(old_pref)

    new_pref = _make_pref(
        tenant_id=t,
        intent="Change my notification to 15 minutes",
        parameters={"lead_time_minutes": 15},
        supersedes=old_pref.id,
    )
    await store.add_preference(new_pref)

    # Mark old as superseded
    old_pref.status = "superseded"
    old_pref.superseded_by = new_pref.id
    await store.save_preference(old_pref)

    # Old is superseded, new is active
    old = await store.get_preference(t, old_pref.id)
    assert old.status == "superseded"
    assert old.superseded_by == new_pref.id

    new = await store.get_preference(t, new_pref.id)
    assert new.status == "active"
    assert new.supersedes == old_pref.id

    # Query active returns only new
    active = await store.query_preferences(t)
    assert len(active) == 1
    assert active[0].id == new_pref.id


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


async def test_query_by_scope(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(tenant_id=t, scope="global"))
    await store.add_preference(_make_pref(tenant_id=t, scope="space_music"))

    global_prefs = await store.query_preferences(t, scope="global")
    assert len(global_prefs) == 1

    space_prefs = await store.query_preferences(t, scope="space_music")
    assert len(space_prefs) == 1


async def test_query_by_subject(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(tenant_id=t, subject="calendar"))
    await store.add_preference(_make_pref(tenant_id=t, subject="email"))

    cal = await store.query_preferences(t, subject="calendar")
    assert len(cal) == 1
    assert cal[0].subject == "calendar"


async def test_query_by_category(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(tenant_id=t, category="notification"))
    await store.add_preference(_make_pref(tenant_id=t, category="behavior"))

    notif = await store.query_preferences(t, category="notification")
    assert len(notif) == 1


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


async def test_revoke_preference(store):
    t = "sms:+15555550100"
    pref = _make_pref(tenant_id=t)
    await store.add_preference(pref)

    pref.status = "revoked"
    pref.updated_at = utc_now()
    await store.save_preference(pref)

    loaded = await store.get_preference(t, pref.id)
    assert loaded.status == "revoked"

    # Not in active query
    active = await store.query_preferences(t)
    assert len(active) == 0

    # In status-specific query
    revoked = await store.query_preferences(t, status="revoked", active_only=False)
    assert len(revoked) == 1


# ---------------------------------------------------------------------------
# Derived artifacts
# ---------------------------------------------------------------------------


async def test_derived_artifact_tracking(store):
    t = "sms:+15555550100"
    pref = _make_pref(
        tenant_id=t,
        derived_trigger_ids=["trig_abc", "trig_def"],
        derived_covenant_ids=["rule_xyz"],
    )
    await store.add_preference(pref)

    loaded = await store.get_preference(t, pref.id)
    assert loaded.derived_trigger_ids == ["trig_abc", "trig_def"]
    assert loaded.derived_covenant_ids == ["rule_xyz"]


# ---------------------------------------------------------------------------
# Migration from KnowledgeEntry
# ---------------------------------------------------------------------------


async def test_migration_from_knowledge_entries(store):
    """Existing category='preference' KnowledgeEntries get migrated to Preferences."""
    from kernos.kernel.state import KnowledgeEntry

    t = "sms:+15555550100"

    # Create a category="preference" KnowledgeEntry
    ke = KnowledgeEntry(
        id="know_pref01",
        tenant_id=t,
        category="preference",
        subject="calendar_events",
        content="Notify me 4 minutes before appointments",
        confidence="stated",
        source_event_id="",
        source_description="user stated",
        created_at="2026-03-15T10:00:00Z",
        last_referenced="",
        tags=[],
    )
    await store.add_knowledge(ke)

    # First preference access triggers migration
    prefs = await store.query_preferences(t)
    assert len(prefs) == 1
    assert prefs[0].intent == "Notify me 4 minutes before appointments"
    assert prefs[0].subject == "calendar_events"
    assert prefs[0].source_knowledge_id == "know_pref01"

    # Original KnowledgeEntry marked as migrated
    original = await store.get_knowledge_entry(t, "know_pref01")
    assert original.category == "preference_migrated"


async def test_migration_idempotent(store):
    """Migration only runs once per tenant — second access doesn't duplicate."""
    from kernos.kernel.state import KnowledgeEntry

    t = "sms:+15555550100"
    ke = KnowledgeEntry(
        id="know_pref02",
        tenant_id=t,
        category="preference",
        subject="email",
        content="Summarize emails daily",
        confidence="stated",
        source_event_id="",
        source_description="user stated",
        created_at="2026-03-15T10:00:00Z",
        last_referenced="",
        tags=[],
    )
    await store.add_knowledge(ke)

    prefs1 = await store.query_preferences(t)
    prefs2 = await store.query_preferences(t)
    assert len(prefs1) == len(prefs2) == 1


async def test_no_migration_when_no_preference_entries(store):
    """No crash or empty prefs when there are no preference KnowledgeEntries."""
    t = "sms:+15555550100"
    prefs = await store.query_preferences(t)
    assert prefs == []


# ---------------------------------------------------------------------------
# generate_preference_id
# ---------------------------------------------------------------------------


def test_generate_preference_id_format():
    pid = generate_preference_id()
    assert pid.startswith("pref_")
    assert len(pid) == 13  # "pref_" + 8 hex chars


# ---------------------------------------------------------------------------
# Persistence survives reload
# ---------------------------------------------------------------------------


async def test_persistence_survives_reload(tmp_path):
    store1 = JsonStateStore(str(tmp_path))
    pref = _make_pref()
    await store1.add_preference(pref)

    store2 = JsonStateStore(str(tmp_path))
    loaded = await store2.get_preference(pref.tenant_id, pref.id)
    assert loaded is not None
    assert loaded.id == pref.id
    assert loaded.intent == pref.intent
