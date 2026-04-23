"""Phase modules for the turn pipeline (HANDLER-PIPELINE-DECOMPOSE).

Every phase exposes a single entry point::

    async def run(ctx: PhaseContext) -> PhaseContext: ...

Phases do not reach around each other. Every cross-phase communication
rides the :class:`~kernos.messages.phase_context.PhaseContext` dataclass.
Services (state store, reasoning, instance_db, etc.) are reached via
``ctx.handler`` — a back-reference to the orchestrating MessageHandler
populated by ``process()`` at turn start.

Pipeline order: provision → route → assemble → reason → consequence → persist.
"""
from kernos.messages.phases import (
    assemble,
    consequence,
    persist,
    provision,
    reason,
    route,
)

__all__ = [
    "assemble",
    "consequence",
    "persist",
    "provision",
    "reason",
    "route",
]
