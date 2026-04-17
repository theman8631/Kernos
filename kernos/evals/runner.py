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
from typing import Any

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
) -> ScenarioResult:
    """Run a scenario end-to-end and return its result.

    compaction_threshold: if provided, forces KERNOS_COMPACTION_THRESHOLD so
        compaction fires earlier (needed for scenarios that verify post-compaction state).
    background_task_wait_s: after each turn, wait this long for tier2 extraction
        and other asyncio.create_task work to settle.
    """
    result = ScenarioResult(scenario=scenario, started_at=utc_now())
    bi: BootstrappedInstance | None = None

    try:
        # --- Setup ---
        bi = await bootstrap_instance(
            scenario.setup, compaction_threshold=compaction_threshold,
        )
        attach_setup_members(bi, scenario.setup.members)
        result.setup_summary = _summarize_setup(scenario, bi)

        # --- Turns ---
        for idx, turn in enumerate(scenario.turns, start=1):
            turn_result = await _run_turn(
                bi, idx, turn, background_task_wait_s=background_task_wait_s,
            )
            result.turn_results.append(turn_result)
            if turn_result.error:
                logger.warning("eval turn %d errored: %s", idx, turn_result.error)

        # --- Observations ---
        result.observations = await _capture_observations(bi, scenario.observations)

        # --- Rubrics ---
        result.rubric_verdicts = await evaluate_rubrics(
            bi.reasoning, scenario.rubrics, result,
        )

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

    # Action turns (wipe, claim_code, etc.) are not yet implemented — skip cleanly.
    if turn.action:
        return TurnResult(
            turn_index=idx,
            sender_display=display,
            content=f"[action: {turn.action}]",
            reply="",
            error=f"action turns not yet supported: {turn.action}",
        )

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
