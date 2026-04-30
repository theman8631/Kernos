"""Receipt-ack timing (CRB AC #27).

The principal cohort acks an inbound Drafter signal when it has
**durably ingested** the signal, NOT when it speaks to the user.
Speech may happen turns later. This module factors the ack-emission
helper so engine bring-up wires it once and the principal-cohort
fan-out runner calls it after queue commit.

Receipt event type: ``drafter.receipt.signal_acknowledged`` (defined
in Drafter v2 receipts.py; emitted via Drafter's emitter, not CRB's).

The ack carries the ``signal_id`` (the original event_id of the
inbound Drafter signal) so the receipt-timeout diagnostic can
correlate the round-trip.
"""
from __future__ import annotations

from typing import Awaitable, Callable

# Drafter's receipt taxonomy — re-imported here for the ack helper.
from kernos.kernel.cohorts.drafter.receipts import (
    RECEIPT_SIGNAL_ACKNOWLEDGED,
    build_signal_acknowledged_payload,
)


# A signal-acknowledger callable: takes the signal_id of the inbound
# event, emits drafter.receipt.signal_acknowledged via the Drafter
# event emitter, and returns the receipt's event_id.
PrincipalReceiptAcknowledger = Callable[[str], "Awaitable[str]"]


def make_signal_acknowledger(
    *,
    drafter_emitter: "DrafterEventPort",  # type: ignore[name-defined] # forward reference
    instance_id: str,
) -> PrincipalReceiptAcknowledger:
    """Build a closure that emits drafter.receipt.signal_acknowledged.

    Engine bring-up calls this once per principal cohort instance and
    hands the resulting callable to the cohort's per-turn fan-out
    runner. The runner invokes it AFTER it has durably committed the
    inbound signal to its queue (Path A) or AFTER cursor.commit_position
    has succeeded (Path B).

    The acknowledger takes a ``signal_id`` and returns the substrate-
    set ``event_id`` of the receipt for diagnostic correlation.
    """

    async def _ack(signal_id: str) -> str:
        if not signal_id:
            raise ValueError("signal_id is required")
        payload = build_signal_acknowledged_payload(signal_id=signal_id)
        # The emit_receipt method on DrafterEventPort returns a dict
        # via action_log.record_and_perform; the substrate event_id
        # lives inside. For v1 the signal_id is the correlation key
        # the diagnostic check uses; the receipt's own event_id is
        # not load-bearing. Return the signal_id as the canonical
        # correlation handle.
        await drafter_emitter.emit_receipt(
            source_event_id=signal_id,
            receipt_type=RECEIPT_SIGNAL_ACKNOWLEDGED,
            payload=payload,
            target_id=f"signal_acknowledged::{signal_id}",
        )
        return signal_id

    return _ack


__all__ = [
    "PrincipalReceiptAcknowledger",
    "make_signal_acknowledger",
]
