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
        instance_id="sms:+15555550100",
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

    loaded = await store.get_preference(pref.instance_id, pref.id)
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
    await store.add_preference(_make_pref(instance_id=t, subject="calendar_events"))
    await store.add_preference(_make_pref(instance_id=t, subject="email"))
    await store.add_preference(_make_pref(instance_id=t, subject="responses", status="revoked"))

    active = await store.query_preferences(t, active_only=True)
    assert len(active) == 2

    all_prefs = await store.query_preferences(t, active_only=False)
    assert len(all_prefs) == 3


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


async def test_supersession(store):
    t = "sms:+15555550100"
    old_pref = _make_pref(instance_id=t, parameters={"lead_time_minutes": 10})
    await store.add_preference(old_pref)

    new_pref = _make_pref(
        instance_id=t,
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
    await store.add_preference(_make_pref(instance_id=t, scope="global"))
    await store.add_preference(_make_pref(instance_id=t, scope="space_music"))

    global_prefs = await store.query_preferences(t, scope="global")
    assert len(global_prefs) == 1

    space_prefs = await store.query_preferences(t, scope="space_music")
    assert len(space_prefs) == 1


async def test_query_by_subject(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(instance_id=t, subject="calendar"))
    await store.add_preference(_make_pref(instance_id=t, subject="email"))

    cal = await store.query_preferences(t, subject="calendar")
    assert len(cal) == 1
    assert cal[0].subject == "calendar"


async def test_query_by_category(store):
    t = "sms:+15555550100"
    await store.add_preference(_make_pref(instance_id=t, category="notification"))
    await store.add_preference(_make_pref(instance_id=t, category="behavior"))

    notif = await store.query_preferences(t, category="notification")
    assert len(notif) == 1


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


async def test_revoke_preference(store):
    t = "sms:+15555550100"
    pref = _make_pref(instance_id=t)
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
        instance_id=t,
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
        instance_id=t,
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
    """Migration only runs once per_instance — second access doesn't duplicate."""
    from kernos.kernel.state import KnowledgeEntry

    t = "sms:+15555550100"
    ke = KnowledgeEntry(
        id="know_pref02",
        instance_id=t,
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
    loaded = await store2.get_preference(pref.instance_id, pref.id)
    assert loaded is not None
    assert loaded.id == pref.id
    assert loaded.intent == pref.intent


# ---------------------------------------------------------------------------
# Preference Linkage (SPEC-6A-2)
# ---------------------------------------------------------------------------

from kernos.kernel.preference_reconcile import (
    reconcile_preference_change,
    classify_preference_change,
)
from kernos.kernel.scheduler import Trigger, TriggerStore


async def test_source_preference_id_on_trigger(tmp_path):
    """AC1: Trigger has source_preference_id field."""
    trigger_store = TriggerStore(str(tmp_path))
    t = Trigger(
        trigger_id="trig_test01",
        instance_id="sms:+15555550100",
        condition_type="time",
        condition="every day at 9am",
        action_type="notify",
        action_description="Daily reminder",
        source_preference_id="pref_abc12345",
    )
    await trigger_store.save(t)
    loaded = await trigger_store.get("sms:+15555550100", "trig_test01")
    assert loaded.source_preference_id == "pref_abc12345"


async def test_source_preference_id_on_covenant(tmp_path):
    """AC2: CovenantRule has source_preference_id field."""
    from kernos.kernel.state import CovenantRule
    store = JsonStateStore(str(tmp_path))
    rule = CovenantRule(
        id="rule_test01",
        instance_id="sms:+15555550100",
        capability="general",
        rule_type="preference",
        description="Keep responses short",
        active=True,
        source="user_stated",
        created_at=utc_now(),
        source_preference_id="pref_xyz99999",
    )
    await store.add_contract_rule(rule)
    rules = await store.get_contract_rules("sms:+15555550100")
    matched = [r for r in rules if r.id == "rule_test01"]
    assert len(matched) == 1
    assert matched[0].source_preference_id == "pref_xyz99999"


async def test_legacy_trigger_has_empty_source_preference_id(tmp_path):
    """AC10: Legacy triggers without linkage continue to work."""
    trigger_store = TriggerStore(str(tmp_path))
    t = Trigger(
        trigger_id="trig_legacy",
        instance_id="sms:+15555550100",
        condition_type="time",
        condition="once",
        action_type="notify",
        action_description="Old trigger",
    )
    await trigger_store.save(t)
    loaded = await trigger_store.get("sms:+15555550100", "trig_legacy")
    assert loaded.source_preference_id == ""


async def test_revocation_cascade_deactivates_trigger(tmp_path):
    """AC7: Revoking a preference deactivates linked triggers."""
    store = JsonStateStore(str(tmp_path))
    trigger_store = TriggerStore(str(tmp_path))
    t = "sms:+15555550100"

    trigger = Trigger(
        trigger_id="trig_linked",
        instance_id=t,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Calendar notification",
        source_preference_id="pref_revoke01",
        status="active",
    )
    await trigger_store.save(trigger)

    pref = _make_pref(
        id="pref_revoke01",
        instance_id=t,
        status="revoked",
        derived_trigger_ids=["trig_linked"],
    )

    result = await reconcile_preference_change(
        pref, store, trigger_store, "revoke",
    )
    assert result is True

    loaded = await trigger_store.get(t, "trig_linked")
    assert loaded.status == "paused"


async def test_revocation_cascade_deactivates_covenant(tmp_path):
    """AC7: Revoking a preference deactivates linked covenants."""
    from kernos.kernel.state import CovenantRule
    store = JsonStateStore(str(tmp_path))
    t = "sms:+15555550100"

    rule = CovenantRule(
        id="rule_linked01",
        instance_id=t,
        capability="general",
        rule_type="preference",
        description="Allowed to send proactive SMS",
        active=True,
        source="user_stated",
        created_at=utc_now(),
        source_preference_id="pref_revoke02",
    )
    await store.add_contract_rule(rule)

    pref = _make_pref(
        id="pref_revoke02",
        instance_id=t,
        status="revoked",
        derived_covenant_ids=["rule_linked01"],
    )

    result = await reconcile_preference_change(
        pref, store, None, "revoke",
    )
    assert result is True

    rules = await store.get_contract_rules(t, active_only=False)
    matched = [r for r in rules if r.id == "rule_linked01"]
    assert len(matched) == 1
    assert matched[0].active is False


async def test_parameter_update_modifies_trigger_in_place(tmp_path):
    """AC4: Parameter-preserving changes update linked triggers in place."""
    store = JsonStateStore(str(tmp_path))
    trigger_store = TriggerStore(str(tmp_path))
    t = "sms:+15555550100"

    trigger = Trigger(
        trigger_id="trig_param01",
        instance_id=t,
        condition_type="time",
        condition="before event",
        action_type="notify",
        action_description="Notify 4 minutes before",
        action_params={"lead_time_minutes": 4},
        source_preference_id="pref_param01",
        status="active",
    )
    await trigger_store.save(trigger)

    pref = _make_pref(
        id="pref_param01",
        instance_id=t,
        intent="Notify me 10 minutes before appointments",
        parameters={"lead_time_minutes": 10},
        derived_trigger_ids=["trig_param01"],
    )

    result = await reconcile_preference_change(
        pref, store, trigger_store, "parameter_update",
    )
    assert result is True

    loaded = await trigger_store.get(t, "trig_param01")
    assert loaded.action_params["lead_time_minutes"] == 10
    assert loaded.status == "active"  # Same object, not retired


async def test_supersession_cascade(tmp_path):
    """AC8: Superseding a preference deactivates old derived objects."""
    store = JsonStateStore(str(tmp_path))
    trigger_store = TriggerStore(str(tmp_path))
    t = "sms:+15555550100"

    old_trigger = Trigger(
        trigger_id="trig_old",
        instance_id=t,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Old notification",
        source_preference_id="pref_old",
        status="active",
    )
    await trigger_store.save(old_trigger)

    old_pref = _make_pref(
        id="pref_old",
        instance_id=t,
        status="superseded",
        derived_trigger_ids=["trig_old"],
    )
    new_pref = _make_pref(
        id="pref_new",
        instance_id=t,
        supersedes="pref_old",
    )

    result = await reconcile_preference_change(
        new_pref, store, trigger_store, "supersede", old_preference=old_pref,
    )
    assert result is True

    loaded = await trigger_store.get(t, "trig_old")
    assert loaded.status == "paused"


async def test_stale_marker_on_failure(tmp_path):
    """AC9: Failed reconciliation returns False (stale marker)."""
    from unittest.mock import AsyncMock

    store = JsonStateStore(str(tmp_path))

    # Use a trigger store that fails on save
    bad_trigger_store = AsyncMock()
    bad_trigger_store.get.side_effect = RuntimeError("store unavailable")
    bad_trigger_store.list_all = AsyncMock(return_value=[])

    pref = _make_pref(
        derived_trigger_ids=["trig_broken"],
    )
    await store.add_preference(pref)

    result = await reconcile_preference_change(
        pref, store, bad_trigger_store, "revoke",
    )
    assert result is False  # Stale — reconciliation failed


def test_classify_preference_change_parameter():
    """AC6: Parameter change correctly classified."""
    result = classify_preference_change(
        old_params={"lead_time": 4}, new_params={"lead_time": 10},
        old_action="notify", new_action="notify",
        old_category="notification", new_category="notification",
    )
    assert result == "parameter_update"


def test_classify_preference_change_structural():
    """AC5: Structural intent change correctly classified."""
    result = classify_preference_change(
        old_params={}, new_params={},
        old_action="notify", new_action="schedule",
        old_category="notification", new_category="notification",
    )
    assert result == "structural_replace"

    result2 = classify_preference_change(
        old_params={}, new_params={},
        old_action="notify", new_action="notify",
        old_category="notification", new_category="behavior",
    )
    assert result2 == "structural_replace"
