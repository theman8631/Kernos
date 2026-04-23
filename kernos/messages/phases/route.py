"""Route phase — invoke the router cohort, assign active_space_id.

HANDLER-PIPELINE-DECOMPOSE. Delegates to ``ctx.handler._phase_route(ctx)``
during the verbatim-move migration.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext


async def run(ctx: PhaseContext) -> PhaseContext:
    """Run the route phase on ``ctx`` and return it."""
    await ctx.handler._phase_route(ctx)
    return ctx
