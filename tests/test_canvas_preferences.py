"""CANVAS-GARDENER-PREFERENCE-CAPTURE Commit 1 — storage helpers.

CanvasService methods for preferences / pending_preferences / declined_
preferences in canvas.yaml. Pure storage surface; extraction + dispatch
integration ship in later commits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from kernos.kernel.canvas import CanvasService, canvas_dir
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_preftest"
OPERATOR = "member:inst_preftest:owner"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="T", scope="personal",
    )
    yield svc, idb, tmp_path, c.canvas_id
    await idb.close()


# ---- get_preferences / set_preference / remove_preference -----------------


async def test_get_preferences_returns_empty_when_absent(env):
    svc, _, _, canvas_id = env
    assert await svc.get_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    ) == {}


async def test_set_preference_persists_to_yaml(env):
    svc, _, tmp_path, canvas_id = env
    ok = await svc.set_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", value="threshold-only",
    )
    assert ok
    # Read directly from disk to prove persistence.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    assert data["preferences"]["rsvp-routing"] == "threshold-only"


async def test_set_preference_overwrites_existing(env):
    svc, _, _, canvas_id = env
    await svc.set_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", value="threshold-only",
    )
    await svc.set_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", value="every-rsvp",
    )
    prefs = await svc.get_preferences(instance_id=INSTANCE, canvas_id=canvas_id)
    assert prefs["rsvp-routing"] == "every-rsvp"


async def test_remove_preference(env):
    svc, _, _, canvas_id = env
    await svc.set_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="x", value="y",
    )
    ok = await svc.remove_preference(
        instance_id=INSTANCE, canvas_id=canvas_id, name="x",
    )
    assert ok
    prefs = await svc.get_preferences(instance_id=INSTANCE, canvas_id=canvas_id)
    assert "x" not in prefs


async def test_remove_preference_is_idempotent(env):
    svc, _, _, canvas_id = env
    # Remove without ever setting — should not raise, returns True.
    assert await svc.remove_preference(
        instance_id=INSTANCE, canvas_id=canvas_id, name="never-existed",
    )


# ---- pending_preferences + resolve_pending_preference ---------------------


async def test_add_pending_preference_stamps_surfaced_at(env):
    svc, _, _, canvas_id = env
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={
            "name": "rsvp-routing",
            "value": "threshold-only",
            "effect_kind": "suppression",
            "evidence": "don't spam me with every RSVP",
            "confidence": "high",
        },
    )
    pending = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert len(pending) == 1
    assert pending[0]["name"] == "rsvp-routing"
    # surfaced_at auto-stamped.
    assert pending[0].get("surfaced_at")


async def test_add_pending_preference_replaces_same_name(env):
    """Adding a pending preference with a name already pending replaces, not
    appends. Prevents duplicate-pending on re-extraction of the same utterance."""
    svc, _, _, canvas_id = env
    base = {
        "name": "rsvp-routing", "value": "threshold-only",
        "effect_kind": "suppression", "confidence": "high",
    }
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id, preference=base,
    )
    base_v2 = dict(base)
    base_v2["value"] = "silent"
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id, preference=base_v2,
    )
    pending = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert len(pending) == 1
    assert pending[0]["value"] == "silent"


async def test_resolve_pending_preference_confirm(env):
    svc, _, _, canvas_id = env
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={
            "name": "rsvp-routing", "value": "threshold-only",
            "effect_kind": "suppression", "confidence": "high",
        },
    )
    result = await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", action="confirm",
    )
    assert result == {"action": "confirm", "name": "rsvp-routing",
                       "value": "threshold-only"}
    # Pending list is empty; preferences reflects confirmed value.
    assert await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    ) == []
    prefs = await svc.get_preferences(instance_id=INSTANCE, canvas_id=canvas_id)
    assert prefs["rsvp-routing"] == "threshold-only"


async def test_resolve_pending_preference_discard_records_decline(env):
    svc, _, tmp_path, canvas_id = env
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={
            "name": "rsvp-routing", "value": "threshold-only",
            "effect_kind": "suppression", "confidence": "high",
            "evidence": "don't spam me",
        },
    )
    result = await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", action="discard",
    )
    assert result["action"] == "discard"
    # Not in confirmed preferences.
    assert "rsvp-routing" not in await svc.get_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    # Recorded in declined_preferences with evidence preserved.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    declined = data.get("declined_preferences") or []
    assert any(d["name"] == "rsvp-routing" and d["evidence"] == "don't spam me"
                for d in declined)


async def test_resolve_pending_preference_unknown_returns_none(env):
    svc, _, _, canvas_id = env
    result = await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="never-added", action="confirm",
    )
    assert result is None


async def test_resolve_pending_preference_invalid_action_returns_none(env):
    svc, _, _, canvas_id = env
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "x", "value": "y", "effect_kind": "suppression",
                    "confidence": "high"},
    )
    assert await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="x", action="not-a-valid-action",
    ) is None


# ---- drop_expired_pending_preferences -------------------------------------


async def test_drop_expired_pending_removes_old_entries(env):
    svc, _, tmp_path, canvas_id = env
    # Add two pending prefs; backdate one past the TTL.
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "fresh", "value": "v", "effect_kind": "suppression",
                    "confidence": "high"},
    )
    # Directly mutate the yaml to add an old-timestamp entry.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    data["pending_preferences"].append({
        "name": "stale", "value": "v", "effect_kind": "suppression",
        "confidence": "high", "surfaced_at": old_ts,
    })
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    dropped = await svc.drop_expired_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id, ttl_hours=24,
    )
    assert dropped == 1
    remaining = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert [p["name"] for p in remaining] == ["fresh"]


async def test_drop_expired_pending_noop_when_all_fresh(env):
    svc, _, _, canvas_id = env
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "x", "value": "y", "effect_kind": "suppression",
                    "confidence": "high"},
    )
    dropped = await svc.drop_expired_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id, ttl_hours=24,
    )
    assert dropped == 0


# ---- supersession --------------------------------------------------------


async def test_supersession_via_new_utterance(env):
    """Preference supersession: new pending with supersedes field names the
    prior preference. On confirm, old is overwritten."""
    svc, _, _, canvas_id = env
    # Confirm an initial preference.
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "rsvp-routing", "value": "threshold-only",
                    "effect_kind": "suppression", "confidence": "high"},
    )
    await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", action="confirm",
    )
    assert (await svc.get_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    ))["rsvp-routing"] == "threshold-only"

    # New pending with supersedes references the old.
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "rsvp-routing", "value": "every-rsvp",
                    "effect_kind": "suppression", "confidence": "high",
                    "supersedes": "rsvp-routing"},
    )
    await svc.resolve_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        name="rsvp-routing", action="confirm",
    )
    # Confirmed preference now reflects the new value.
    prefs = await svc.get_preferences(instance_id=INSTANCE, canvas_id=canvas_id)
    assert prefs["rsvp-routing"] == "every-rsvp"


# ---- mutation helper robustness ------------------------------------------


async def test_mutate_on_missing_canvas_returns_false(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    ok = await svc.set_preference(
        instance_id="nope", canvas_id="does_not_exist",
        name="x", value="y",
    )
    assert ok is False
    await idb.close()
