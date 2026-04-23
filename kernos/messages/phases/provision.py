"""Provision phase — resolve instance_id, member, per-member state.

HANDLER-PIPELINE-DECOMPOSE. During the verbatim-move migration this
module delegates to ``ctx.handler._phase_provision(ctx)`` so the
monolith remains the single source of truth. Subsequent commits migrate
the body out of the handler into this module directly.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the provision phase on ``ctx`` and return it."""
    await ctx.handler._phase_provision(ctx)
    return ctx
