"""Synthetic test cohort fixture.

Per acceptance criterion #11: a fixture cohort that demonstrates
the registration + execution path end-to-end. Used by both the
runner test suite and the live test.

Parameterised so a test can construct cohorts with predictable
behaviours: succeed with a payload, raise, hang past timeout. The
fixture is the canonical smoke-test cohort for subsequent specs
too — when a real cohort adapter (gardener, memory, patterns,
covenant) lands, its tests can use this fixture as a known-good
sibling to verify the runner's handling of mixed-outcome fan-outs.
"""

from __future__ import annotations

import asyncio
from enum import Enum

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortDescriptor,
    ExecutionMode,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Public,
    Visibility,
)


class SyntheticBehaviour(str, Enum):
    """How a synthetic cohort should behave when fired.

    SUCCEED — return a CohortOutput with the configured payload.
    HANG    — `await asyncio.sleep(delay_seconds)` past timeout_ms;
              the runner produces a TIMEOUT_PER_COHORT (or
              TIMEOUT_GLOBAL if the global cap also fires).
    RAISE   — raise RuntimeError(error_message).
    """

    SUCCEED = "succeed"
    HANG = "hang"
    RAISE = "raise"


def make_synthetic_cohort(
    cohort_id: str,
    *,
    behaviour: SyntheticBehaviour = SyntheticBehaviour.SUCCEED,
    payload: dict | None = None,
    delay_seconds: float = 0.0,
    error_message: str = "synthetic failure",
    required: bool = False,
    safety_class: bool = False,
    timeout_ms: int = 1000,
    default_visibility: Visibility | None = None,
) -> CohortDescriptor:
    """Build a CohortDescriptor whose run callable matches `behaviour`.

    The default payload is `{"synthetic": True, "cohort_id": ...}`
    so tests can confirm at a glance whether a cohort produced its
    own output vs a runner-synthesised one.

    `delay_seconds` lets a SUCCEED cohort sleep before returning
    (useful for ordering / wall-clock tests) and lets a HANG cohort
    set how long it sleeps past its timeout. The HANG path uses
    `delay_seconds` directly; if zero, defaults to
    `timeout_ms / 1000 + 0.5` so the timeout always fires.
    """
    visibility = default_visibility or Public()
    body = payload if payload is not None else {
        "synthetic": True,
        "cohort_id": cohort_id,
    }

    async def _run(ctx: CohortContext) -> CohortOutput:
        if behaviour is SyntheticBehaviour.HANG:
            wait = delay_seconds or (timeout_ms / 1000.0) + 0.5
            await asyncio.sleep(wait)
            return _ok(ctx, cohort_id, body, visibility)
        if behaviour is SyntheticBehaviour.RAISE:
            raise RuntimeError(error_message)
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return _ok(ctx, cohort_id, body, visibility)

    return CohortDescriptor(
        cohort_id=cohort_id,
        run=_run,
        timeout_ms=timeout_ms,
        default_visibility=visibility,
        required=required,
        safety_class=safety_class,
        execution_mode=ExecutionMode.ASYNC,
    )


def _ok(
    ctx: CohortContext,
    cohort_id: str,
    payload: dict,
    visibility: Visibility,
) -> CohortOutput:
    # cohort_run_id placeholder — the runner mints the canonical id.
    return CohortOutput(
        cohort_id=cohort_id,
        cohort_run_id=f"{ctx.turn_id}:{cohort_id}:provisional",
        output=dict(payload),
        visibility=visibility,
    )


__all__ = [
    "SyntheticBehaviour",
    "make_synthetic_cohort",
]
