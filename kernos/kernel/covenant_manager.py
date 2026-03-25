"""Covenant management — LLM-based validation, manage_covenants tool, startup migration.

Handles:
- Post-write LLM validation of the full covenant set (MERGE/CONFLICT/REWRITE/NO_ISSUES)
- manage_covenants kernel tool (list/remove/update — management only, not creation)
- One-time startup migration to clean existing data (word overlap, zero LLM cost)

Rule creation is owned exclusively by Tier 2 extraction → NL contract parser.
The agent does NOT create rules — it manages existing ones via manage_covenants.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from kernos.kernel.state import CovenantRule, StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

MANAGE_COVENANTS_TOOL = {
    "name": "manage_covenants",
    "description": (
        "View, update, or remove behavioral rules (covenants). "
        "Use when the user asks about their rules, wants to change "
        "a rule, or remove one. Do NOT use this to create new rules — "
        "behavioral instructions are automatically captured by the kernel."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "remove", "update"],
                "description": "What to do: list rules, remove a rule, or update a rule's description.",
            },
            "rule_id": {
                "type": "string",
                "description": "The rule ID (required for remove/update). Shown in list output.",
            },
            "new_description": {
                "type": "string",
                "description": "New description for the rule (required for update).",
            },
            "show_all": {
                "type": "boolean",
                "description": "Include superseded/removed rules in listing (default false).",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Structured output schema for validation
# ---------------------------------------------------------------------------

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["MERGE", "CONFLICT", "REWRITE", "SUPERSEDE", "NO_ISSUES"],
                    },
                    "retire_rule_id": {"type": "string"},
                    "keep_rule_id": {"type": "string"},
                    "supersede_rule_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "rule_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "rule_id": {"type": "string"},
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                    "current_description": {"type": "string"},
                    "suggested_description": {"type": "string"},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["actions"],
    "additionalProperties": False,
}


# Track conflict pairs to suppress re-logging and auto-resolve after 3 runs.
# Key: frozenset({rule_id_a, rule_id_b}), Value: run count
_conflict_tracker: dict[frozenset, int] = {}
_CONFLICT_AUTO_SUPERSEDE_THRESHOLD = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Post-write LLM validation of the full covenant set
# ---------------------------------------------------------------------------

_VALIDATION_SYSTEM_PROMPT = """\
You are a covenant rule validator for a personal AI assistant. You will receive \
a set of active behavioral rules. Your job is to identify problems and recommend \
actions.

For each problem found, return ONE action:

- SUPERSEDE: A newer rule on the same topic replaces an older one — the user \
changed their mind. Return retire_rule_id (the older rule to remove) and \
keep_rule_id (the newer one that stays). This is the most common resolution \
when two rules seem to conflict. Example: "be verbose about system events" \
followed later by "don't narrate successful tool calls" = SUPERSEDE the older.

- MERGE: Two or more rules say the same thing in different words. Keep the best-\
worded version, supersede the others. Return the keep_rule_id and the \
supersede_rule_ids, plus a reason.

- CONFLICT: Two rules GENUINELY contradict each other AND it's ambiguous which \
one the user prefers. This is rare — most apparent conflicts are actually \
SUPERSEDE (user changed their mind). Only use CONFLICT when you truly cannot \
tell which instruction is newer or preferred. Return both rule_ids and a \
plain-English description.

- REWRITE: A single rule is poorly worded, vague, or could be clearer. Return \
the rule_id, current_description, and a suggested_description.

- NO_ISSUES: The covenant set is clean. No action needed.

Be thorough. Check every rule against every other rule. Prefer SUPERSEDE over \
CONFLICT when two rules address the same topic with different instructions — \
the user most likely changed their mind.

Respond with JSON only. No other text."""


async def validate_covenant_set(
    state: StateStore,
    events,
    reasoning_service,
    tenant_id: str,
    new_rule_id: str,
    awareness_state: StateStore | None = None,
) -> dict:
    """Post-write LLM validation of the full covenant set.

    Fires after every covenant rule creation/update.
    Identifies conflicts, merges, and rewrites via a single Haiku call.
    Auto-resolves merges and rewrites. Creates whisper for conflicts.

    Returns: {"merges": int, "conflicts": int, "rewrites": int}
    """
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import emit_event

    stats = {"merges": 0, "conflicts": 0, "rewrites": 0, "supersedes": 0}

    try:
        active_rules = await state.get_contract_rules(tenant_id, active_only=True)
        if len(active_rules) <= 1:
            return stats

        # Build rule list for the LLM
        rule_lines = []
        rule_map: dict[str, CovenantRule] = {}
        for rule in active_rules:
            scope = rule.context_space or "global"
            created = rule.created_at[:19] if rule.created_at else "unknown"
            rule_lines.append(
                f"- rule_id: {rule.id}\n"
                f"  type: {rule.rule_type.upper()}\n"
                f"  description: {rule.description}\n"
                f"  context_space: {scope}\n"
                f"  created_at: {created}"
            )
            rule_map[rule.id] = rule

        user_msg = (
            "Active covenant rules for this tenant:\n\n"
            + "\n".join(rule_lines)
            + f"\n\nThe most recently written rule is: {new_rule_id}\n\n"
            "Identify any SUPERSEDE, MERGE, CONFLICT, or REWRITE issues. "
            "If the set is clean, return NO_ISSUES."
        )

        result = await reasoning_service.complete_simple(
            system_prompt=_VALIDATION_SYSTEM_PROMPT,
            user_content=user_msg,
            output_schema=VALIDATION_SCHEMA,
            max_tokens=1024,
            prefer_cheap=True,
        )

        parsed = json.loads(result)
        actions = parsed.get("actions", [])

        for action in actions:
            action_type = action.get("type", "")

            if action_type == "NO_ISSUES":
                logger.info(
                    "COVENANT_VALIDATE: set clean (%d active rules)",
                    len(active_rules),
                )
                continue

            if action_type == "SUPERSEDE":
                await _handle_supersede(
                    state, events, tenant_id, action, rule_map, stats,
                )

            elif action_type == "MERGE":
                await _handle_merge(
                    state, events, tenant_id, action, rule_map, stats,
                )

            elif action_type == "CONFLICT":
                await _handle_conflict(
                    state, events, tenant_id, action, rule_map, stats,
                )

            elif action_type == "REWRITE":
                await _handle_rewrite(
                    state, events, tenant_id, action, rule_map, stats,
                )

    except Exception as exc:
        logger.warning("COVENANT_VALIDATE: failed (%s), skipping", exc)

    return stats


async def _handle_supersede(
    state: StateStore,
    events,
    tenant_id: str,
    action: dict,
    rule_map: dict[str, CovenantRule],
    stats: dict,
) -> None:
    """Auto-execute a SUPERSEDE — newer rule retires the older one."""
    from kernos.kernel.events import emit_event

    retire_id = action.get("retire_rule_id", "")
    keep_id = action.get("keep_rule_id", "")
    reason = action.get("reason", "")

    # If LLM returned rule_ids instead, pick older/newer by created_at
    if not retire_id and not keep_id:
        rule_ids = action.get("rule_ids", [])
        valid = [rid for rid in rule_ids if rid in rule_map]
        if len(valid) >= 2:
            sorted_by_age = sorted(valid, key=lambda rid: rule_map[rid].created_at or "")
            retire_id = sorted_by_age[0]  # older
            keep_id = sorted_by_age[-1]   # newer

    if not retire_id or retire_id not in rule_map:
        logger.warning("COVENANT_VALIDATE: SUPERSEDE skipped — retire_rule_id %r not found", retire_id)
        return
    if not keep_id or keep_id not in rule_map:
        logger.warning("COVENANT_VALIDATE: SUPERSEDE skipped — keep_rule_id %r not found", keep_id)
        return

    # Verify: retire the older rule. If timestamps disagree with LLM's choice, trust timestamps.
    retire_rule = rule_map[retire_id]
    keep_rule = rule_map[keep_id]
    if retire_rule.created_at and keep_rule.created_at and retire_rule.created_at > keep_rule.created_at:
        # LLM got it backwards — swap
        retire_id, keep_id = keep_id, retire_id
        retire_rule, keep_rule = keep_rule, retire_rule

    await state.update_contract_rule(
        tenant_id, retire_id,
        {"superseded_by": keep_id, "updated_at": _now_iso()},
    )

    stats["supersedes"] += 1
    logger.info(
        "COVENANT_WRITE: id=%s action=SUPERSEDE source=validate_covenant_set trigger=superseded_by:%s reason=%s",
        retire_id, keep_id, reason[:80],
    )

    try:
        await emit_event(
            events, "covenant.rule.superseded", tenant_id,
            "covenant_validator",
            payload={
                "retired_rule_id": retire_id,
                "kept_rule_id": keep_id,
                "reason": reason,
            },
        )
    except Exception as exc:
        logger.warning("Failed to emit covenant.rule.superseded: %s", exc)


async def _handle_merge(
    state: StateStore,
    events,
    tenant_id: str,
    action: dict,
    rule_map: dict[str, CovenantRule],
    stats: dict,
) -> None:
    """Auto-execute a MERGE action."""
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import emit_event

    keep_id = action.get("keep_rule_id", "")
    supersede_ids = action.get("supersede_rule_ids", [])
    reason = action.get("reason", "")

    if not keep_id or keep_id not in rule_map:
        logger.warning("COVENANT_VALIDATE: MERGE skipped — keep_rule_id %r not found", keep_id)
        return

    valid_supersede = [rid for rid in supersede_ids if rid in rule_map and rid != keep_id]
    if not valid_supersede:
        logger.warning("COVENANT_VALIDATE: MERGE skipped — no valid supersede_rule_ids")
        return

    for rid in valid_supersede:
        await state.update_contract_rule(
            tenant_id, rid,
            {"superseded_by": keep_id, "updated_at": _now_iso()},
        )

    stats["merges"] += len(valid_supersede)
    logger.info(
        "COVENANT_VALIDATE: merged %d duplicates into %s: %s",
        len(valid_supersede), keep_id, reason,
    )

    try:
        await emit_event(
            events, EventType.COVENANT_RULE_MERGED, tenant_id,
            "covenant_validator",
            payload={
                "kept_rule_id": keep_id,
                "superseded_rule_ids": valid_supersede,
                "reason": reason,
            },
        )
    except Exception as exc:
        logger.warning("Failed to emit covenant.rule.merged: %s", exc)


async def _handle_conflict(
    state: StateStore,
    events,
    tenant_id: str,
    action: dict,
    rule_map: dict[str, CovenantRule],
    stats: dict,
) -> None:
    """Handle a CONFLICT — track, suppress re-logging, auto-supersede after threshold."""
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import emit_event
    from kernos.kernel.awareness import Whisper, generate_whisper_id

    rule_ids = action.get("rule_ids", [])
    description = action.get("description", "")

    valid_ids = [rid for rid in rule_ids if rid in rule_map]
    if len(valid_ids) < 2:
        logger.warning("COVENANT_VALIDATE: CONFLICT skipped — need at least 2 valid rule_ids")
        return

    # Track conflict pair to suppress re-logging
    pair_key = frozenset(valid_ids[:2])
    run_count = _conflict_tracker.get(pair_key, 0) + 1
    _conflict_tracker[pair_key] = run_count

    # Auto-supersede after threshold: retire the older rule
    if run_count >= _CONFLICT_AUTO_SUPERSEDE_THRESHOLD:
        sorted_by_age = sorted(valid_ids[:2], key=lambda rid: rule_map[rid].created_at or "")
        retire_id = sorted_by_age[0]
        keep_id = sorted_by_age[1]

        await state.update_contract_rule(
            tenant_id, retire_id,
            {"superseded_by": keep_id, "updated_at": _now_iso()},
        )
        _conflict_tracker.pop(pair_key, None)

        stats["supersedes"] = stats.get("supersedes", 0) + 1
        logger.info(
            "COVENANT_WRITE: id=%s action=AUTO_SUPERSEDE source=conflict_default "
            "trigger=unresolved_after_%d_runs keep=%s",
            retire_id, _CONFLICT_AUTO_SUPERSEDE_THRESHOLD, keep_id,
        )
        return

    # First occurrence: create whisper and log
    if run_count == 1:
        stats["conflicts"] += 1
        logger.info(
            "COVENANT_VALIDATE: conflict detected between %s: %s",
            valid_ids, description,
        )

        evidence = []
        for rid in valid_ids:
            rule = rule_map[rid]
            evidence.append(f"Rule {rid} [{rule.rule_type.upper()}]: {rule.description}")

        whisper = Whisper(
            whisper_id=generate_whisper_id(),
            insight_text=(
                f"I have two rules pulling in different directions: {description} "
                "I'll go with the newer one — let me know if you want to adjust."
            ),
            delivery_class="stage",
            source_space_id="",
            target_space_id="",
            supporting_evidence=evidence,
            reasoning_trace="Detected by post-write covenant validation",
            knowledge_entry_id="",
            foresight_signal="covenant_conflict",
            created_at=_now_iso(),
        )

        try:
            await state.save_whisper(tenant_id, whisper)
        except Exception as exc:
            logger.warning("COVENANT_VALIDATE: failed to save conflict whisper: %s", exc)
    else:
        # Subsequent runs before threshold — suppress logging
        return

    try:
        await emit_event(
            events, EventType.COVENANT_CONTRADICTION_DETECTED, tenant_id,
            "covenant_validator",
            payload={
                "rule_ids": valid_ids,
                "description": description,
                "whisper_created": True,
            },
        )
    except Exception as exc:
        logger.warning("Failed to emit covenant.contradiction.detected: %s", exc)


async def _handle_rewrite(
    state: StateStore,
    events,
    tenant_id: str,
    action: dict,
    rule_map: dict[str, CovenantRule],
    stats: dict,
) -> None:
    """Auto-execute a REWRITE action."""
    from dataclasses import replace as _replace
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import emit_event

    rule_id = action.get("rule_id", "")
    suggested = action.get("suggested_description", "")

    if not rule_id or rule_id not in rule_map:
        logger.warning("COVENANT_VALIDATE: REWRITE skipped — rule_id %r not found", rule_id)
        return
    if not suggested:
        logger.warning("COVENANT_VALIDATE: REWRITE skipped — empty suggested_description")
        return

    old_rule = rule_map[rule_id]
    now = _now_iso()
    new_id = f"rule_{uuid4().hex[:8]}"
    new_rule = _replace(
        old_rule,
        id=new_id,
        description=suggested,
        supersedes=rule_id,
        superseded_by="",
        created_at=now,
        updated_at=now,
        version=old_rule.version + 1,
    )
    await state.add_contract_rule(new_rule)
    await state.update_contract_rule(
        tenant_id, rule_id,
        {"superseded_by": new_id, "updated_at": now},
    )

    stats["rewrites"] += 1
    logger.info(
        "COVENANT_VALIDATE: rewrote %s: %r → %r",
        rule_id, old_rule.description[:60], suggested[:60],
    )

    try:
        await emit_event(
            events, EventType.COVENANT_RULE_UPDATED, tenant_id,
            "covenant_validator",
            payload={
                "old_rule_id": rule_id,
                "new_rule_id": new_id,
                "old_description": old_rule.description,
                "new_description": suggested,
            },
        )
    except Exception as exc:
        logger.warning("Failed to emit covenant.rule.updated (rewrite): %s", exc)


# ---------------------------------------------------------------------------
# Supersession helper (used by coordinator and cleanup)
# ---------------------------------------------------------------------------


async def supersede_rules(
    state: StateStore,
    tenant_id: str,
    rules_to_supersede: list[CovenantRule],
    superseded_by: str,
) -> None:
    """Mark rules as superseded."""
    for rule in rules_to_supersede:
        await state.update_contract_rule(
            tenant_id,
            rule.id,
            {"superseded_by": superseded_by, "updated_at": _now_iso()},
        )


# ---------------------------------------------------------------------------
# manage_covenants tool handler
# ---------------------------------------------------------------------------


async def handle_manage_covenants(
    state: StateStore,
    tenant_id: str,
    action: str,
    rule_id: str = "",
    new_description: str = "",
    show_all: bool = False,
) -> str:
    """Handle the manage_covenants kernel tool."""
    if action == "list":
        return await _list_covenants(state, tenant_id, show_all)
    elif action == "remove":
        if not rule_id:
            return "Error: rule_id is required for remove action."
        return await _remove_covenant(state, tenant_id, rule_id)
    elif action == "update":
        if not rule_id:
            return "Error: rule_id is required for update action."
        if not new_description:
            return "Error: new_description is required for update action."
        return await _update_covenant(state, tenant_id, rule_id, new_description)
    else:
        return f"Error: Unknown action '{action}'. Use 'list', 'remove', or 'update'."


async def _list_covenants(state: StateStore, tenant_id: str, show_all: bool) -> str:
    """List covenant rules grouped by type."""
    if show_all:
        all_rules = await state.get_contract_rules(tenant_id, active_only=False)
    else:
        all_rules = await state.get_contract_rules(tenant_id, active_only=True)

    if not all_rules:
        return "No covenant rules found."

    groups: dict[str, list[str]] = {}
    for rule in all_rules:
        label = rule.rule_type.upper()
        status = ""
        if rule.superseded_by:
            status = f" [SUPERSEDED by {rule.superseded_by}]"
        scope = f" (space: {rule.context_space})" if rule.context_space else ""
        source_tag = f" [{rule.source}]" if rule.source != "default" else ""
        entry = f"  - [{rule.id}] {rule.description}{scope}{source_tag}{status}"
        groups.setdefault(label, []).append(entry)

    lines = ["**Standing Covenant Rules:**\n"]
    for group_name in ["MUST_NOT", "MUST", "PREFERENCE", "ESCALATION"]:
        if group_name in groups:
            lines.append(f"**{group_name}:**")
            lines.extend(groups[group_name])
            lines.append("")

    for group_name, entries in groups.items():
        if group_name not in ["MUST_NOT", "MUST", "PREFERENCE", "ESCALATION"]:
            lines.append(f"**{group_name}:**")
            lines.extend(entries)
            lines.append("")

    lines.append(f"Total: {len(all_rules)} rules")
    return "\n".join(lines)


async def _remove_covenant(state: StateStore, tenant_id: str, rule_id: str) -> str:
    """Soft-remove a covenant rule."""
    rules = await state.get_contract_rules(tenant_id, active_only=False)
    target = None
    for r in rules:
        if r.id == rule_id:
            target = r
            break

    if not target:
        return f"Error: Rule '{rule_id}' not found."
    if target.superseded_by:
        return f"Rule '{rule_id}' is already removed/superseded."

    await state.update_contract_rule(
        tenant_id, rule_id,
        {"superseded_by": "user_removed", "updated_at": _now_iso()},
    )
    logger.info("COVENANT_REMOVE: rule=%s desc=%r", rule_id, target.description)
    return f"Removed rule '{rule_id}': {target.description}"


async def _update_covenant(
    state: StateStore, tenant_id: str, rule_id: str, new_description: str,
) -> str:
    """Update a covenant rule by creating a new one and superseding the old."""
    from dataclasses import replace as _replace

    rules = await state.get_contract_rules(tenant_id, active_only=False)
    target = None
    for r in rules:
        if r.id == rule_id:
            target = r
            break

    if not target:
        return f"Error: Rule '{rule_id}' not found."
    if target.superseded_by:
        return f"Rule '{rule_id}' is already removed/superseded."

    now = _now_iso()
    new_id = f"rule_{uuid4().hex[:8]}"
    new_rule = _replace(
        target,
        id=new_id,
        description=new_description,
        supersedes=rule_id,
        superseded_by="",
        created_at=now,
        updated_at=now,
        version=target.version + 1,
    )
    await state.add_contract_rule(new_rule)

    await state.update_contract_rule(
        tenant_id, rule_id,
        {"superseded_by": new_id, "updated_at": now},
    )
    logger.info(
        "COVENANT_UPDATE: old=%s new=%s desc=%r",
        rule_id, new_id, new_description[:80],
    )
    return (
        f"Updated rule: '{target.description}' → '{new_description}' "
        f"(new ID: {new_id})"
    )


# ---------------------------------------------------------------------------
# Startup migration — clean existing data (zero LLM cost)
# ---------------------------------------------------------------------------


async def run_covenant_cleanup(
    state: StateStore,
    tenant_id: str,
    embedding_service=None,
    reasoning_service=None,
) -> dict:
    """One-time migration to deduplicate and resolve contradictions.

    Uses word overlap only (no LLM cost). The LLM validation handles
    everything else at write time.

    Returns stats: {"deduped": int, "contradictions_resolved": int}
    """
    from kernos.kernel.contract_parser import compute_word_overlap

    all_rules = await state.get_contract_rules(tenant_id, active_only=False)
    active_rules = [r for r in all_rules if not r.superseded_by and r.active]

    stats = {"deduped": 0, "contradictions_resolved": 0}

    # Phase 1: Deduplicate — group by (rule_type, word overlap > 0.80)
    seen: list[CovenantRule] = []
    for rule in active_rules:
        matched_idx = -1
        for i, existing in enumerate(seen):
            if existing.rule_type != rule.rule_type:
                continue

            sim = compute_word_overlap(existing.description, rule.description)
            if sim > 0.80:
                older, newer = (existing, rule) if existing.created_at <= rule.created_at else (rule, existing)
                await state.update_contract_rule(
                    tenant_id, older.id,
                    {"superseded_by": newer.id, "updated_at": _now_iso()},
                )
                stats["deduped"] += 1
                logger.info(
                    "COVENANT_CLEANUP: deduped %s (sim=%.3f) superseded by %s",
                    older.id, sim, newer.id,
                )
                seen[i] = newer
                matched_idx = i
                break

        if matched_idx < 0:
            seen.append(rule)

    # Phase 2: Contradiction detection — MUST vs MUST_NOT with word overlap > 0.70
    all_rules = await state.get_contract_rules(tenant_id, active_only=False)
    active_rules = [r for r in all_rules if not r.superseded_by and r.active]

    musts = [r for r in active_rules if r.rule_type == "must"]
    must_nots = [r for r in active_rules if r.rule_type == "must_not"]

    for must_rule in musts:
        for must_not_rule in must_nots:
            if must_not_rule.superseded_by:
                continue

            sim = compute_word_overlap(must_rule.description, must_not_rule.description)
            if sim < 0.70:
                continue

            older, newer = (
                (must_not_rule, must_rule)
                if must_not_rule.created_at <= must_rule.created_at
                else (must_rule, must_not_rule)
            )

            await state.update_contract_rule(
                tenant_id, older.id,
                {"superseded_by": newer.id, "updated_at": _now_iso()},
            )
            stats["contradictions_resolved"] += 1
            logger.info(
                "COVENANT_CLEANUP: contradiction resolved %s superseded by %s (sim=%.3f)",
                older.id, newer.id, sim,
            )

    if stats["deduped"] or stats["contradictions_resolved"]:
        logger.info(
            "COVENANT_CLEANUP: tenant=%s deduped=%d contradictions=%d",
            tenant_id, stats["deduped"], stats["contradictions_resolved"],
        )

    return stats
