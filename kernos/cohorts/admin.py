"""Admin-tool handlers for the Messenger cohort.

System-space-only. Enforced at dispatch time by the ReasoningService tool
loop (same pattern as ``set_chain_model`` and ``diagnose_llm_chain``).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def diagnose_messenger(
    *,
    instance_id: str,
    member_a_id: str,
    member_b_id: str,
    state,
    instance_db,
) -> dict:
    """Return a readable view of what the Messenger sees for a pair.

    Not a history of past decisions — Messenger outcomes are friction-trace
    only and never persisted to an event stream by contract. The tool
    instead surfaces the *current judgment inputs* for the pair: the
    pair-scoped covenants, unexpired ephemeral permissions, and the
    relationship profile on each side.
    """
    out: dict = {
        "ok": True,
        "member_a": {"id": member_a_id},
        "member_b": {"id": member_b_id},
    }

    # Display names.
    try:
        prof_a = await instance_db.get_member_profile(member_a_id)
        out["member_a"]["display_name"] = (
            (prof_a or {}).get("display_name", "") or member_a_id
        )
    except Exception:
        out["member_a"]["display_name"] = member_a_id
    try:
        prof_b = await instance_db.get_member_profile(member_b_id)
        out["member_b"]["display_name"] = (
            (prof_b or {}).get("display_name", "") or member_b_id
        )
    except Exception:
        out["member_b"]["display_name"] = member_b_id

    # Relationship profiles on each side (RM permission matrix).
    try:
        out["relationship_a_to_b"] = await instance_db.get_permission(
            member_a_id, member_b_id,
        ) or "unknown"
    except Exception:
        out["relationship_a_to_b"] = "unknown"
    try:
        out["relationship_b_to_a"] = await instance_db.get_permission(
            member_b_id, member_a_id,
        ) or "unknown"
    except Exception:
        out["relationship_b_to_a"] = "unknown"

    # Covenants scoped to member_a as disclosing, member_b as target (or
    # relationship profile match).
    def _covenant_matches(rule, disclosing: str, requesting: str, relationship: str) -> bool:
        if not getattr(rule, "active", True):
            return False
        owner = getattr(rule, "member_id", "") or ""
        if owner and owner != disclosing:
            return False
        target = getattr(rule, "target", "") or ""
        if target and target != requesting and target != relationship:
            return False
        return True

    try:
        rules = await state.get_contract_rules(instance_id)
    except Exception as exc:
        rules = []
        out["covenants_error"] = str(exc)

    out["covenants_a_as_disclosing"] = [
        {
            "id": getattr(r, "id", ""),
            "rule_type": getattr(r, "rule_type", ""),
            "description": getattr(r, "description", ""),
            "topic": getattr(r, "topic", "") or "",
            "target": getattr(r, "target", "") or "",
        }
        for r in rules
        if _covenant_matches(r, member_a_id, member_b_id, out["relationship_a_to_b"])
    ]
    out["covenants_b_as_disclosing"] = [
        {
            "id": getattr(r, "id", ""),
            "rule_type": getattr(r, "rule_type", ""),
            "description": getattr(r, "description", ""),
            "topic": getattr(r, "topic", "") or "",
            "target": getattr(r, "target", "") or "",
        }
        for r in rules
        if _covenant_matches(r, member_b_id, member_a_id, out["relationship_b_to_a"])
    ]

    # Ephemeral permissions for each direction.
    try:
        eph_a = await state.list_ephemeral_permissions(
            instance_id,
            disclosing_member_id=member_a_id,
            requesting_member_id=member_b_id,
        )
        out["ephemeral_permissions_a_to_b"] = [
            {
                "id": p.id, "topic": p.topic, "granted": p.granted,
                "created_at": p.created_at, "expires_at": p.expires_at,
            }
            for p in eph_a
        ]
    except Exception:
        out["ephemeral_permissions_a_to_b"] = []
    try:
        eph_b = await state.list_ephemeral_permissions(
            instance_id,
            disclosing_member_id=member_b_id,
            requesting_member_id=member_a_id,
        )
        out["ephemeral_permissions_b_to_a"] = [
            {
                "id": p.id, "topic": p.topic, "granted": p.granted,
                "created_at": p.created_at, "expires_at": p.expires_at,
            }
            for p in eph_b
        ]
    except Exception:
        out["ephemeral_permissions_b_to_a"] = []

    return out
