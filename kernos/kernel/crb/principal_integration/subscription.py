"""Principal cohort subscription wiring (Seam C8).

The spec calls for conditional discovery between two paths:

* **Path A** — extend an existing durable principal cohort
  ingestion mechanism (per-turn fan-out queue, etc.).
* **Path B** — adopt the universal ``cohorts/_substrate/cursor.py``
  substrate with ``cohort_id="principal"``.

Either path preserves the invariant: ack means **durable ingestion**,
NOT speech. Receipt acknowledgment fires after the principal queue
has accepted the signal, even though the principal may speak about
it turns later.

Discovery (CC's batch-report decision): no existing principal
durable mechanism present in this codebase. The principal cohort is
a per-turn fan-out runner that reads from the live event_stream API
rather than maintaining an independent cursor. v1 ships **Path B**.

The :class:`PrincipalSubscriptionAdapter` accepts a custom ingest
callable for callers that need Path A — engine bring-up can wire a
queue.put-and-ack closure if a future refactor adds one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Iterable

from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)

logger = logging.getLogger(__name__)


# Drafter signal types the principal cohort subscribes to. Matches
# Drafter v2 + v1.1 + v1.2 surface; adding a new signal type is a
# deliberate change here so additions surface at engine bring-up.
PRINCIPAL_SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset({
    "drafter.signal.draft_ready",
    "drafter.signal.gap_detected",
    "drafter.signal.multi_intent_detected",
    "drafter.signal.idle_resurface",
    "drafter.signal.draft_paused",
    "drafter.signal.draft_abandoned",
})


class DiscoveredPath(str, Enum):
    """Result of :func:`discover_subscription_path`."""

    PATH_A_EXISTING = "path_a_existing"
    PATH_B_CURSOR_ADOPTED = "path_b_cursor_adopted"


@dataclass(frozen=True)
class PrincipalSubscription:
    """The wired subscription. Returned from
    :meth:`PrincipalSubscriptionAdapter.start`.

    Test pin: ``path`` records which discovery branch was taken so
    the batch report can flag it.
    """

    path: DiscoveredPath
    cohort_id: str
    instance_id: str
    event_types: tuple[str, ...]
    cursor: DurableEventCursor | None = None
    ingest: Callable[[dict], "Awaitable[None]"] | None = None


def discover_subscription_path(
    *,
    has_existing_durable_mechanism: bool,
) -> DiscoveredPath:
    """Determine which path to take. v1 uses a simple boolean
    discovery — engine bring-up tells the adapter whether an existing
    principal cohort durable mechanism is present.

    Future refactors that add a Path A mechanism flip the flag here;
    no spec amendment needed."""
    if has_existing_durable_mechanism:
        return DiscoveredPath.PATH_A_EXISTING
    return DiscoveredPath.PATH_B_CURSOR_ADOPTED


class PrincipalSubscriptionAdapter:
    """Wires the principal cohort to the Drafter signal stream.

    Path A: caller supplies an ``existing_ingest`` callable that
    enqueues into the existing durable mechanism.

    Path B: adapter constructs a :class:`DurableEventCursor` over
    ``cohort_id="principal"`` + ``instance_id`` + the Drafter signal
    types. Receipt-ack timing fires on cursor commit (which happens
    after the cohort processes the event).
    """

    def __init__(
        self,
        *,
        cursor_store: CursorStore,
        cohort_id: str = "principal",
        event_types: frozenset[str] = PRINCIPAL_SUBSCRIBED_EVENT_TYPES,
    ) -> None:
        self._cursor_store = cursor_store
        self._cohort_id = cohort_id
        self._event_types = event_types

    async def start(
        self,
        *,
        instance_id: str,
        has_existing_durable_mechanism: bool = False,
        existing_ingest: Callable[[dict], "Awaitable[None]"] | None = None,
    ) -> PrincipalSubscription:
        """Wire the subscription per Seam C8.

        Args:
            instance_id: scope.
            has_existing_durable_mechanism: True if the engine already
                wires a principal queue (Path A). Default False —
                Path B (adopt cursor substrate).
            existing_ingest: Path A callable. Required when
                ``has_existing_durable_mechanism=True``.
        """
        path = discover_subscription_path(
            has_existing_durable_mechanism=has_existing_durable_mechanism,
        )
        if path == DiscoveredPath.PATH_A_EXISTING:
            if existing_ingest is None:
                raise ValueError(
                    "Path A requires existing_ingest callable"
                )
            return PrincipalSubscription(
                path=path,
                cohort_id=self._cohort_id,
                instance_id=instance_id,
                event_types=tuple(sorted(self._event_types)),
                cursor=None,
                ingest=existing_ingest,
            )
        # Path B: adopt the universal cursor substrate.
        cursor = DurableEventCursor(
            cursor_store=self._cursor_store,
            cohort_id=self._cohort_id,
            instance_id=instance_id,
            event_types=self._event_types,
        )
        return PrincipalSubscription(
            path=path,
            cohort_id=self._cohort_id,
            instance_id=instance_id,
            event_types=tuple(sorted(self._event_types)),
            cursor=cursor,
            ingest=None,
        )


__all__ = [
    "DiscoveredPath",
    "PRINCIPAL_SUBSCRIBED_EVENT_TYPES",
    "PrincipalSubscription",
    "PrincipalSubscriptionAdapter",
    "discover_subscription_path",
]
