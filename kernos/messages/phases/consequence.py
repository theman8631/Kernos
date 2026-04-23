"""Consequence phase — confirmation replay, tool config persistence, projectors.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_consequence``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from kernos.kernel.reasoning import ReasoningRequest
from kernos.messages.phase_context import PhaseContext

logger = logging.getLogger(__name__)


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 5: Confirmation replay, tool config, projectors, soul update."""
    # Lazy imports avoid circular import on handler helpers
    from kernos.kernel.projectors.coordinator import run_projectors
    from kernos.messages.handler import _maybe_append_name_ask

    handler = ctx.handler
    instance_id = ctx.instance_id
    request = ReasoningRequest(
        instance_id=instance_id, conversation_id=ctx.conversation_id,
        system_prompt=ctx.system_prompt, messages=ctx.messages, tools=ctx.tools,
        system_prompt_static=ctx.system_prompt_static,
        system_prompt_dynamic=ctx.system_prompt_dynamic,
        model="", trigger="", active_space_id=ctx.active_space_id,
        member_id=ctx.member_id,
        input_text=ctx.message.content, active_space=ctx.active_space,
    )

    # Confirmation replay
    pending = handler.reasoning.get_pending_actions(instance_id)
    conflict_this_turn = handler.reasoning.get_conflict_raised()
    if pending and conflict_this_turn:
        confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
        ctx.response_text = confirm_pattern.sub("", ctx.response_text).strip()
        logger.info("CONFIRM_BLOCKED: instance=%s reason=same_turn_as_conflict", instance_id)
    elif pending:
        confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
        matches = confirm_pattern.findall(ctx.response_text)
        if matches:
            actions_to_execute: list[int] = []
            for match in matches:
                if match.upper() == "ALL":
                    actions_to_execute = list(range(len(pending)))
                    break
                else:
                    idx = int(match)
                    if 0 <= idx < len(pending) and idx not in actions_to_execute:
                        actions_to_execute.append(idx)
            execution_results: list[str] = []
            for idx in actions_to_execute:
                action = pending[idx]
                if datetime.now(timezone.utc) < action.expires_at:
                    try:
                        result = await handler.reasoning.execute_tool(action.tool_name, action.tool_input, request)
                        execution_results.append(f"✓ {action.proposed_action}: {result}")
                        logger.info("CONFIRM_EXECUTE: tool=%s idx=%d", action.tool_name, idx)
                    except Exception as exc:
                        execution_results.append(f"Failed: {action.proposed_action} ({exc})")
                        logger.warning("CONFIRM_EXECUTE_FAILED: tool=%s idx=%d error=%s", action.tool_name, idx, exc)
                else:
                    execution_results.append(f"Expired: {action.proposed_action}")
                    logger.warning("CONFIRM_EXPIRED: tool=%s idx=%d", action.tool_name, idx)
            handler.reasoning.clear_pending_actions(instance_id)
            ctx.response_text = confirm_pattern.sub("", ctx.response_text).strip()
            if execution_results:
                ctx.response_text += "\n\n" + "\n".join(execution_results)
        else:
            all_expired = all(datetime.now(timezone.utc) >= a.expires_at for a in pending)
            if all_expired:
                handler.reasoning.clear_pending_actions(instance_id)
                logger.info("PENDING_CLEARED: instance=%s reason=all_expired", instance_id)

    # Tool config persistence
    if handler.reasoning.get_tools_changed():
        handler.reasoning.reset_tools_changed()
        try:
            await handler._persist_mcp_config(instance_id)
            system_space = await handler._get_system_space(instance_id)
            if system_space:
                await handler._write_capabilities_overview(instance_id, system_space.id)
        except Exception as exc:
            logger.warning("Failed to persist tools config: %s", exc)

    # Projectors
    history = await handler.conversations.get_recent(instance_id, ctx.conversation_id, limit=20)
    await run_projectors(
        user_message=ctx.message.content, recent_turns=history[-4:],
        soul=ctx.soul, state=handler.state, events=handler.events,
        reasoning_service=handler.reasoning, instance_id=instance_id,
        active_space_id=ctx.active_space_id, active_space=ctx.active_space,
        member_id=ctx.member_id, member_profile=ctx.member_profile,
        instance_db=getattr(handler, '_instance_db', None),
    )

    ctx.response_text = _maybe_append_name_ask(ctx.response_text, ctx.soul, member_profile=ctx.member_profile)
    await handler._post_response_soul_update(ctx.soul, member_id=ctx.member_id, member_profile=ctx.member_profile, active_space_id=ctx.active_space_id)

    # Cross-domain signal check — skip for self-directed turns
    if not ctx.is_self_directed:
        try:
            import asyncio as _aio
            _aio.create_task(handler._check_cross_domain_signals(
                ctx.instance_id, ctx.active_space_id,
                ctx.message.content or "", ctx.response_text))
        except Exception:
            pass
    return ctx
