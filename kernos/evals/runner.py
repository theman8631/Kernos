"""Scenario runner — executes a parsed Scenario against an isolated handler.

Produces a ScenarioResult with turn-by-turn transcripts, captured observations,
and rubric verdicts. The report module turns this into readable markdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from kernos.evals.bootstrap import (
    BootstrappedInstance, attach_setup_members, bootstrap_instance, build_message,
)
from kernos.evals.rubrics import evaluate_rubrics
from kernos.evals.types import (
    Observation, Rubric, Scenario, ScenarioResult, Turn, TurnResult,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


async def run_scenario(
    scenario: Scenario,
    compaction_threshold: int | None = None,
    background_task_wait_s: float = 3.0,
    on_event: Callable[[str, dict], None] | None = None,
) -> ScenarioResult:
    """Run a scenario end-to-end and return its result.

    compaction_threshold: if provided, forces KERNOS_COMPACTION_THRESHOLD so
        compaction fires earlier (needed for scenarios that verify post-compaction state).
    background_task_wait_s: after each turn, wait this long for tier2 extraction
        and other asyncio.create_task work to settle.
    on_event: optional callback(kind, payload) for progress reporting.
        Kinds: "setup_done", "turn_done", "observations_done", "rubrics_done".
    """
    result = ScenarioResult(scenario=scenario, started_at=utc_now())
    bi: BootstrappedInstance | None = None
    total_turns = len(scenario.turns)

    def _emit(kind: str, payload: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:  # progress must never break the run
            logger.debug("on_event callback failed", exc_info=True)

    try:
        # --- Setup ---
        bi = await bootstrap_instance(
            scenario.setup, compaction_threshold=compaction_threshold,
        )
        attach_setup_members(bi, scenario.setup.members)
        result.setup_summary = _summarize_setup(scenario, bi)
        _emit("setup_done", {"scenario": scenario.name})

        # --- Turns ---
        for idx, turn in enumerate(scenario.turns, start=1):
            turn_result = await _run_turn(
                bi, idx, turn, background_task_wait_s=background_task_wait_s,
            )
            result.turn_results.append(turn_result)
            if turn_result.error:
                logger.warning("eval turn %d errored: %s", idx, turn_result.error)
            _emit("turn_done", {
                "scenario": scenario.name,
                "index": idx, "total": total_turns,
                "duration_ms": turn_result.duration_ms,
                "error": turn_result.error,
                "sender": turn_result.sender_display,
            })

        # --- Observations ---
        result.observations = await _capture_observations(bi, scenario.observations)
        _emit("observations_done", {
            "scenario": scenario.name, "count": len(scenario.observations),
        })

        # --- Rubrics ---
        result.rubric_verdicts = await evaluate_rubrics(
            bi.reasoning, scenario.rubrics, result,
        )
        _emit("rubrics_done", {
            "scenario": scenario.name, "count": len(scenario.rubrics),
        })

    except Exception as exc:
        tb = traceback.format_exc()
        result.setup_error = f"{exc}\n\n{tb}"
        logger.exception("eval scenario failed")
    finally:
        if bi is not None:
            await bi.close()

    result.completed_at = utc_now()
    return result


async def _run_turn(
    bi: BootstrappedInstance,
    idx: int,
    turn: Turn,
    background_task_wait_s: float,
) -> TurnResult:
    """Run a single turn and capture what happened."""
    display = f"{turn.sender}/{turn.platform}" if turn.platform else f"{turn.sender}"

    # Action turns: small set of harness primitives for scenarios that need
    # to manipulate state directly (crash simulation, expiration backdating).
    if turn.action:
        return await _run_action_turn(bi, idx, turn, display)

    if not turn.platform or not turn.content:
        return TurnResult(
            turn_index=idx, sender_display=display,
            content=turn.content, reply="",
            error="turn missing platform or content",
        )

    msg = build_message(
        bi, sender=turn.sender, platform=turn.platform, content=turn.content,
    )

    # Snapshot running tasks before the turn so we can drain new ones after.
    tasks_before = set(asyncio.all_tasks())
    t0 = time.monotonic()
    reply = ""
    error = ""
    try:
        reply = await bi.handler.process(msg) or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("eval turn process() failed")

    # Drain background tasks spawned during the turn (tier2 extraction, etc.)
    await _drain_new_tasks(tasks_before, timeout=background_task_wait_s)

    duration_ms = int((time.monotonic() - t0) * 1000)

    return TurnResult(
        turn_index=idx,
        sender_display=display,
        content=turn.content,
        reply=reply,
        duration_ms=duration_ms,
        error=error,
    )


async def _run_action_turn(
    bi: BootstrappedInstance, idx: int, turn: Turn, display: str,
) -> TurnResult:
    """Run a harness action turn.

    Supported actions (minimal set, added per scenario needs):

    - `rm_force_state`: directly transition an envelope to a state without
      going through the normal turn pipeline — used to simulate mid-turn
      crashes. Args: message_match (substring of content), to_state.
    - `rm_backdate`: rewrite an envelope's created_at so it exceeds its
      urgency TTL — used to exercise the expiration sweep. Args:
      message_match, seconds_ago.
    - `rm_sweep_expired`: invoke the expiration sweep for the instance.
    """
    action = turn.action.strip()
    args = dict(turn.action_args or {})
    instance_id = _get_instance_id(bi)
    content_preview = f"[action: {action}] args={args}"

    try:
        if action == "rm_force_state":
            await _action_rm_force_state(bi, instance_id, args)
        elif action == "rm_backdate":
            await _action_rm_backdate(bi, instance_id, args)
        elif action == "rm_sweep_expired":
            from kernos.kernel.relational_dispatch import RelationalDispatcher
            dispatcher = bi.handler._get_relational_dispatcher()
            if dispatcher is not None:
                await dispatcher.sweep_expired(instance_id)
        else:
            return TurnResult(
                turn_index=idx, sender_display=display,
                content=content_preview, reply="",
                error=f"unknown action: {action}",
            )
    except Exception as exc:
        return TurnResult(
            turn_index=idx, sender_display=display,
            content=content_preview, reply="",
            error=f"{type(exc).__name__}: {exc}",
        )
    return TurnResult(
        turn_index=idx, sender_display=display,
        content=content_preview, reply=f"action {action} applied",
    )


async def _action_rm_force_state(
    bi: BootstrappedInstance, instance_id: str, args: dict,
) -> None:
    match = args.get("match", "")
    to_state = args.get("to_state", "")
    from_state = args.get("from_state", "")
    if not match or not to_state:
        raise ValueError("rm_force_state needs match= and to_state=")
    rows = await bi.state.query_relational_messages(
        instance_id=instance_id, limit=500,
    )
    target = next((m for m in rows if match in m.content), None)
    if target is None:
        raise ValueError(f"rm_force_state: no envelope matches {match!r}")
    source = from_state or target.state
    ok = await bi.state.transition_relational_message_state(
        instance_id, target.id,
        from_state=source, to_state=to_state,
    )
    if not ok:
        raise RuntimeError(
            f"rm_force_state: CAS lost (from={source!r}, current={target.state!r})"
        )


async def _action_rm_backdate(
    bi: BootstrappedInstance, instance_id: str, args: dict,
) -> None:
    from datetime import datetime, timezone, timedelta
    match = args.get("match", "")
    try:
        seconds_ago = int(args.get("seconds_ago", "0"))
    except (TypeError, ValueError):
        seconds_ago = 0
    if not match or seconds_ago <= 0:
        raise ValueError("rm_backdate needs match= and seconds_ago=<positive int>")
    rows = await bi.state.query_relational_messages(
        instance_id=instance_id, limit=500,
    )
    target = next((m for m in rows if match in m.content), None)
    if target is None:
        raise ValueError(f"rm_backdate: no envelope matches {match!r}")
    new_created = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()
    target.created_at = new_created
    await bi.state.delete_relational_message(instance_id, target.id)
    await bi.state.add_relational_message(target)


async def _drain_new_tasks(tasks_before: set, timeout: float) -> None:
    """Await any new asyncio tasks spawned during the turn, up to timeout."""
    try:
        current = asyncio.current_task()
        new_tasks = (set(asyncio.all_tasks()) - tasks_before) - {current}
        if not new_tasks:
            return
        await asyncio.wait(new_tasks, timeout=timeout)
    except Exception as exc:
        logger.debug("eval: drain_new_tasks: %s", exc)


async def _capture_observations(
    bi: BootstrappedInstance, observations: list[Observation],
) -> dict[str, Any]:
    """Execute each observation directive and collect results."""
    out: dict[str, Any] = {}
    for obs in observations:
        label = obs.label or obs.kind
        try:
            out[label] = await _capture_one(bi, obs)
        except Exception as exc:
            out[label] = {"error": str(exc)}
    return out


async def _capture_one(bi: BootstrappedInstance, obs: Observation) -> Any:
    kind = obs.kind.strip().lower()

    if kind == "member_profile":
        member_ref = obs.args.get("member", "")
        real_id = bi.member_id_map.get(member_ref, member_ref)
        profile = await bi.instance_db.get_member_profile(real_id)
        return profile or {"_missing": True, "looked_up": real_id}

    if kind == "knowledge":
        member_ref = obs.args.get("member", "")
        real_id = bi.member_id_map.get(member_ref, "") if member_ref else ""
        entries = await bi.state.query_knowledge(
            instance_id=_get_instance_id(bi),
            active_only=True,
            limit=500,
            member_id=real_id,
        )
        return [
            {
                "id": e.id,
                "content": e.content,
                "subject": e.subject,
                "category": e.category,
                "sensitivity": getattr(e, "sensitivity", ""),
                "archetype": getattr(e, "lifecycle_archetype", ""),
                "owner_member_id": getattr(e, "owner_member_id", ""),
                "confidence": getattr(e, "confidence", ""),
            }
            for e in entries
        ]

    if kind == "relational_messages":
        # List envelopes where the named member is EITHER origin or addressee.
        # Returns only the fields rubrics verify against (no content leaked
        # to the report beyond what the scenario already pastes in).
        member_ref = obs.args.get("member", "")
        real_id = bi.member_id_map.get(member_ref, member_ref)
        reverse_map = {v: k for k, v in bi.member_id_map.items()}
        instance_id = _get_instance_id(bi)
        as_addressee = await bi.state.query_relational_messages(
            instance_id=instance_id, addressee_member_id=real_id, limit=500,
        )
        as_origin = await bi.state.query_relational_messages(
            instance_id=instance_id, origin_member_id=real_id, limit=500,
        )
        seen_ids: set[str] = set()
        rows: list[dict] = []
        for m in as_addressee + as_origin:
            if m.id in seen_ids:
                continue
            seen_ids.add(m.id)
            rows.append({
                "id": m.id,
                "origin": reverse_map.get(m.origin_member_id, m.origin_member_id),
                "addressee": reverse_map.get(
                    m.addressee_member_id, m.addressee_member_id,
                ),
                "intent": m.intent,
                "urgency": m.urgency,
                "state": m.state,
                "conversation_id": m.conversation_id,
                "content": m.content,
                "target_space_hint": m.target_space_hint,
                "resolution_reason": m.resolution_reason,
                "reply_to_id": m.reply_to_id,
                "created_at": m.created_at,
                "delivered_at": m.delivered_at,
                "surfaced_at": m.surfaced_at,
                "resolved_at": m.resolved_at,
                "expired_at": m.expired_at,
            })
        rows.sort(key=lambda r: r["created_at"])
        return rows

    if kind == "relationships":
        # Directional relationship declarations involving the named member.
        # Lets rubrics verify declaration state without parsing chat text.
        member_ref = obs.args.get("member", "")
        real_id = bi.member_id_map.get(member_ref, member_ref)
        rows = await bi.instance_db.list_relationships(real_id)
        # Re-resolve names using the scenario_id map so rubric text lines up
        # with what the author wrote (e.g. "emma" not "mem_abc123").
        reverse_map = {v: k for k, v in bi.member_id_map.items()}
        out_rows: list[dict] = []
        for r in rows:
            d = {
                "declarer": reverse_map.get(r["declarer_member_id"], r["declarer_member_id"]),
                "other": reverse_map.get(r["other_member_id"], r["other_member_id"]),
                "permission": r.get("permission", "by-permission"),
                "other_display_name": r.get("other_display_name", ""),
            }
            out_rows.append(d)
        return out_rows

    if kind == "covenants":
        rules = await bi.state.get_contract_rules(_get_instance_id(bi))
        return [
            {"id": r.id, "rule_type": r.rule_type, "description": r.description}
            for r in rules if r.active
        ]

    if kind == "conversation_log":
        member_ref = obs.args.get("member", "")
        real_id = bi.member_id_map.get(member_ref, "") if member_ref else ""
        return _read_log_for_member(bi, real_id)

    if kind == "outbound":
        return [
            {
                "channel": r.channel_name,
                "message": r.message,
                "timestamp": r.timestamp,
            }
            for r in bi.outbound
        ]

    return {"_unknown_kind": obs.kind}


def _get_instance_id(bi: BootstrappedInstance) -> str:
    import os
    return os.environ.get("KERNOS_INSTANCE_ID", "eval_instance")


def _read_log_for_member(bi: BootstrappedInstance, member_id: str) -> list[str]:
    """Find the member's conversation log file(s) and return entries as strings."""
    logs: list[str] = []
    base = bi.data_dir / "tenants" / _get_instance_id(bi) / "spaces"
    if not base.exists():
        return logs
    for space_dir in base.iterdir():
        member_logs = space_dir / "members" / member_id / "logs"
        if not member_logs.exists():
            continue
        for log_file in sorted(member_logs.glob("log_*.txt")):
            logs.append(f"[{log_file.name}]\n{log_file.read_text(encoding='utf-8')}")
    return logs


def _summarize_setup(scenario: Scenario, bi: BootstrappedInstance) -> str:
    lines = [f"fresh_instance: {scenario.setup.fresh_instance}"]
    if scenario.setup.members:
        lines.append("members:")
        for m in scenario.setup.members:
            real_id = bi.member_id_map.get(m.id, m.id)
            lines.append(
                f"  - {m.id} ({m.display_name}, {m.role}, {m.platform}) → {real_id}"
            )
    return "\n".join(lines)
