"""Persist phase — conversation log append, events, boundary-triggered cohorts.

HANDLER-PIPELINE-DECOMPOSE. Delegates to ``ctx.handler._phase_persist(ctx)``
during the verbatim-move migration.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the persist phase on ``ctx`` and return it."""
    await ctx.handler._phase_persist(ctx)
    return ctx
