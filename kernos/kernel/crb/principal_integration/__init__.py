"""Principal-cohort integration wiring.

* :mod:`subscription` — Seam C8 conditional resolution. Discovers
  whether the principal cohort already has a durable event-ingestion
  mechanism (Path A) or adopts the universal cohort cursor substrate
  (Path B).
* :mod:`receipt_acks` — emit ack-after-durable-ingest receipts so
  Drafter / CRB can verify the principal queued the signal before
  speech (which may happen turns later).

Path resolution: this codebase has no pre-existing principal cohort
durable cursor mechanism (Drafter v2 introduced the universal
substrate; the principal cohort is a per-turn fan-out runner that
reads from the live event_stream API rather than maintaining an
independent cursor). v1 ships Path B by default — adopt
``cohorts/_substrate/cursor.py`` with ``cohort_id="principal"``,
``instance_id=current_instance``. The receipt-ack invariant
(durable ingest before speech) holds via cursor commit.

Path A remains specified; if a future refactor introduces a separate
principal queue, ``PrincipalSubscriptionAdapter`` accepts a custom
ingest function that bypasses the cursor adoption.
"""
from __future__ import annotations

from kernos.kernel.crb.principal_integration.receipt_acks import (
    PrincipalReceiptAcknowledger,
    make_signal_acknowledger,
)
from kernos.kernel.crb.principal_integration.subscription import (
    DiscoveredPath,
    PrincipalSubscription,
    PrincipalSubscriptionAdapter,
    discover_subscription_path,
)


__all__ = [
    "DiscoveredPath",
    "PrincipalReceiptAcknowledger",
    "PrincipalSubscription",
    "PrincipalSubscriptionAdapter",
    "discover_subscription_path",
    "make_signal_acknowledger",
]
