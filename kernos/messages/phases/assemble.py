"""Assemble phase — build the seven Cognitive UI zones + tool catalog.

HANDLER-PIPELINE-DECOMPOSE. Delegates to ``ctx.handler._phase_assemble(ctx)``
during the verbatim-move migration.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the assemble phase on ``ctx`` and return it."""
    await ctx.handler._phase_assemble(ctx)
    return ctx
