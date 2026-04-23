"""Consequence phase — outbound delivery, relational-message state transitions.

HANDLER-PIPELINE-DECOMPOSE. Delegates to ``ctx.handler._phase_consequence(ctx)``
during the verbatim-move migration.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the consequence phase on ``ctx`` and return it."""
    await ctx.handler._phase_consequence(ctx)
    return ctx
