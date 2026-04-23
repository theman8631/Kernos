"""Reason phase — invoke the principal agent, drive the tool-use loop.

HANDLER-PIPELINE-DECOMPOSE. Delegates to ``ctx.handler._phase_reason(ctx)``
during the verbatim-move migration.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the reason phase on ``ctx`` and return it."""
    await ctx.handler._phase_reason(ctx)
    return ctx
