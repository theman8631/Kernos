"""Preference → Trigger/Covenant reconciliation.

When a Preference changes (update, revoke, supersede), linked
derived objects must update. This module handles the cascade.
"""
import logging
from typing import Any

from kernos.kernel.state import Preference

logger = logging.getLogger(__name__)


async def reconcile_preference_change(
    preference: Preference,
    state: Any,           # StateStore
    trigger_store: Any,   # TriggerStore | None
    change_type: str,     # "parameter_update", "structural_replace", "revoke", "supersede"
    old_preference: Preference | None = None,
) -> bool:
    """Reconcile derived objects after a preference change.

    Returns True if reconciliation succeeded fully. On partial failure,
    marks linked objects as stale and returns False.
    """
    success = True

    if change_type == "revoke":
        success = await _cascade_revoke(preference, state, trigger_store)
    elif change_type == "supersede":
        success = await _cascade_supersede(preference, old_preference, state, trigger_store)
    elif change_type == "parameter_update":
        success = await _cascade_parameter_update(preference, state, trigger_store)
    elif change_type == "structural_replace":
        success = await _cascade_structural_replace(preference, old_preference, state, trigger_store)

    # Update derived artifact IDs on the preference
    try:
        await _refresh_derived_ids(preference, state, trigger_store)
        await state.save_preference(preference)
    except Exception as exc:
        logger.warning("PREF_RECONCILE: failed to refresh derived IDs: %s", exc)
        success = False

    return success


async def _cascade_revoke(
    preference: Preference, state: Any, trigger_store: Any,
) -> bool:
    """Deactivate all derived objects when a preference is revoked."""
    success = True

    # Deactivate linked triggers
    if trigger_store and preference.derived_trigger_ids:
        for tid in preference.derived_trigger_ids:
            try:
                trigger = await trigger_store.get(preference.instance_id, tid)
                if trigger and trigger.status == "active":
                    trigger.status = "paused"
                    await trigger_store.save(trigger)
                    logger.info(
                        "PREF_RECONCILE: trigger=%s paused (preference %s revoked)",
                        tid, preference.id,
                    )
            except Exception as exc:
                logger.warning(
                    "PREF_RECONCILE_STALE: trigger=%s — revocation failed: %s",
                    tid, exc,
                )
                success = False

    # Deactivate linked covenants
    if preference.derived_covenant_ids:
        for cid in preference.derived_covenant_ids:
            try:
                await state.update_contract_rule(
                    preference.instance_id, cid,
                    {"active": False, "superseded_by": f"pref_revoked:{preference.id}"},
                )
                logger.info(
                    "PREF_RECONCILE: covenant=%s deactivated (preference %s revoked)",
                    cid, preference.id,
                )
            except Exception as exc:
                logger.warning(
                    "PREF_RECONCILE_STALE: covenant=%s — revocation failed: %s",
                    cid, exc,
                )
                success = False

    return success


async def _cascade_supersede(
    new_preference: Preference,
    old_preference: Preference | None,
    state: Any,
    trigger_store: Any,
) -> bool:
    """Deactivate old preference's derived objects when superseded."""
    if not old_preference:
        return True
    # Deactivate old preference's derived objects
    return await _cascade_revoke(old_preference, state, trigger_store)


async def _cascade_parameter_update(
    preference: Preference, state: Any, trigger_store: Any,
) -> bool:
    """Update linked triggers/covenants in place for parameter changes."""
    success = True

    # Update linked triggers with new parameters
    if trigger_store and preference.derived_trigger_ids:
        for tid in preference.derived_trigger_ids:
            try:
                trigger = await trigger_store.get(preference.instance_id, tid)
                if trigger and trigger.status == "active":
                    # Merge preference parameters into trigger action_params
                    trigger.action_params.update(preference.parameters)
                    trigger.action_description = preference.intent
                    await trigger_store.save(trigger)
                    logger.info(
                        "PREF_RECONCILE: trigger=%s updated in-place (preference %s params changed)",
                        tid, preference.id,
                    )
            except Exception as exc:
                logger.warning(
                    "PREF_RECONCILE_STALE: trigger=%s — param update failed: %s",
                    tid, exc,
                )
                success = False

    return success


async def _cascade_structural_replace(
    new_preference: Preference,
    old_preference: Preference | None,
    state: Any,
    trigger_store: Any,
) -> bool:
    """Retire old derived objects for structural intent changes.

    New derived objects are NOT auto-created here — that's the job of
    the Preference Parser (6A-4). This just retires the old ones.
    """
    if not old_preference:
        return True
    return await _cascade_revoke(old_preference, state, trigger_store)


async def _refresh_derived_ids(
    preference: Preference, state: Any, trigger_store: Any,
) -> None:
    """Refresh the derived artifact ID lists by querying stores."""
    # Refresh trigger IDs
    if trigger_store:
        try:
            all_triggers = await trigger_store.list_all(preference.instance_id)
            preference.derived_trigger_ids = [
                t.trigger_id for t in all_triggers
                if t.source_preference_id == preference.id
            ]
        except Exception:
            pass  # Keep existing list on failure

    # Refresh covenant IDs
    try:
        rules = await state.query_covenant_rules(preference.instance_id, active_only=False)
        preference.derived_covenant_ids = [
            r.id for r in rules
            if getattr(r, "source_preference_id", "") == preference.id
        ]
    except Exception:
        pass  # Keep existing list on failure


def classify_preference_change(
    old_params: dict, new_params: dict,
    old_action: str, new_action: str,
    old_category: str, new_category: str,
) -> str:
    """Classify whether a preference change is parameter-preserving or structural.

    Returns "parameter_update" or "structural_replace".
    """
    # If action or category changed, it's structural
    if old_action != new_action or old_category != new_category:
        return "structural_replace"
    # Otherwise it's a parameter update
    return "parameter_update"
