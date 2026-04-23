"""Provision phase — resolve instance, member, soul, per-member state.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_provision``.
The body is identical; only ``self.X`` references became ``ctx.handler.X``
so phase modules reach kernel services through the ctx without importing
from handler.py directly.

Responsibilities (unchanged from the monolith):
  - Ensure tenant / soul / MCP config / covenants / evaluator are ready
  - Load the member profile from instance.db; create on first turn
  - One-time migration from Soul per-user fields to member profile
  - Ensure the member has their own default space
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 1: Ensure tenant, soul, MCP config, covenants, evaluator ready."""
    handler = ctx.handler
    instance_id = ctx.instance_id
    message = ctx.message
    await handler.tenants.get_or_create(instance_id)
    await handler._ensureinstance_state(instance_id, message)
    ctx.soul = await handler._get_or_init_soul(instance_id)
    # Load member profile from instance.db
    if ctx.member_id and hasattr(handler, '_instance_db') and handler._instance_db:
        ctx.member_profile = await handler._instance_db.get_member_profile(ctx.member_id)
        if not ctx.member_profile:
            # First turn for this member — create profile from members table
            member = await handler._instance_db.get_member(ctx.member_id)
            if member:
                await handler._instance_db.upsert_member_profile(ctx.member_id, {
                    "display_name": member.get("display_name", ""),
                })
                ctx.member_profile = await handler._instance_db.get_member_profile(ctx.member_id)
        # One-time migration: copy Soul per-user fields to owner's profile
        if ctx.member_profile and ctx.soul:
            soul_fields = {
                "user_name": ctx.soul.user_name,
                "timezone": ctx.soul.timezone,
                "communication_style": ctx.soul.communication_style,
                "interaction_count": ctx.soul.interaction_count,
                "bootstrap_graduated": ctx.soul.bootstrap_graduated,
                "bootstrap_graduated_at": ctx.soul.bootstrap_graduated_at,
                # Soul identity fields (Soul Revision)
                "agent_name": ctx.soul.agent_name,
                "emoji": ctx.soul.emoji,
                "personality_notes": ctx.soul.personality_notes,
                "hatched": ctx.soul.hatched,
                "hatched_at": ctx.soul.hatched_at,
            }
            if any(soul_fields.values()):
                await handler._instance_db.migrate_soul_to_member_profile(
                    ctx.member_id, soul_fields)
                # Reload profile after migration
                ctx.member_profile = await handler._instance_db.get_member_profile(ctx.member_id)
    # Ensure member has their own default space
    if ctx.member_id:
        await handler._ensure_member_default_space(instance_id, ctx.member_id)
    await handler._maybe_load_mcp_config(instance_id)
    await handler._maybe_run_covenant_cleanup(instance_id)
    await handler._maybe_start_evaluator(instance_id)
    return ctx
