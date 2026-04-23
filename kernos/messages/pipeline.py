"""Turn pipeline orchestrator (HANDLER-PIPELINE-DECOMPOSE).

Wires the six phase modules in order. Two entry points:

* :func:`run_lightweight` — provision + route. Runs inline in
  ``MessageHandler.process`` before the per-(instance, space) runner
  mailbox submission.
* :func:`run_heavy` — assemble + reason + consequence + persist. Runs
  inside the space-runner loop for serialized execution of heavy phases.

Kept split because the existing concurrency model (per-(instance,
space) mailbox runners) treats the two halves differently. A future
batch can unify them under a single ``run_turn`` entry once the
runner model is re-scoped.
"""
from __future__ import annotations

from kernos.messages.phase_context import PhaseContext
from kernos.messages.phases import (
    assemble,
    consequence,
    persist,
    provision,
    reason,
    route,
)


#: Pipeline order, lightweight half.
LIGHTWEIGHT_PHASES = (provision, route)

#: Pipeline order, heavy half (serialized through the space runner).
HEAVY_PHASES = (assemble, reason, consequence, persist)

#: Full pipeline, for diagnostics + structural tests.
ALL_PHASES = LIGHTWEIGHT_PHASES + HEAVY_PHASES


async def run_lightweight(ctx: PhaseContext) -> PhaseContext:
    """Run provision + route on ``ctx``. Returns the mutated ctx."""
    for phase in LIGHTWEIGHT_PHASES:
        await phase.run(ctx)
    return ctx


async def run_heavy(ctx: PhaseContext) -> PhaseContext:
    """Run assemble + reason + consequence + persist on ``ctx``. Returns ctx."""
    for phase in HEAVY_PHASES:
        await phase.run(ctx)
    return ctx


async def run_turn(ctx: PhaseContext) -> PhaseContext:
    """Run the full six-phase pipeline on ``ctx``.

    Used by tests and any future caller that wants to bypass the
    space-runner concurrency model (single-turn, single-shot execution).
    Production traffic still flows through ``run_lightweight`` + the
    space runner + ``run_heavy``.
    """
    await run_lightweight(ctx)
    await run_heavy(ctx)
    return ctx
