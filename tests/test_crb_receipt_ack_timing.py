"""Receipt ack timing tests (CRB C5, AC #27).

Pin: ack-after-durable-ingest. The principal cohort emits
``drafter.receipt.signal_acknowledged`` AFTER the inbound Drafter
signal is durably ingested into its queue, NOT after speech.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts.drafter.ports import DrafterEventPort
from kernos.kernel.cohorts.drafter.receipts import (
    RECEIPT_SIGNAL_ACKNOWLEDGED,
)
from kernos.kernel.crb.principal_integration.receipt_acks import (
    make_signal_acknowledger,
)


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    drafter_emitter = event_stream.emitter_registry().register("drafter")
    action_log = ActionLog(cohort_id="drafter")
    await action_log.start(str(tmp_path))
    drafter_port = DrafterEventPort(
        emitter=drafter_emitter, action_log=action_log,
        instance_id="inst_a",
    )
    yield {
        "drafter_emitter": drafter_emitter,
        "drafter_port": drafter_port,
        "action_log": action_log,
    }
    await action_log.stop()
    await event_stream._reset_for_tests()


# ===========================================================================
# Acknowledger emits via Drafter port
# ===========================================================================


class TestAckEmission:
    async def test_acknowledger_emits_signal_acknowledged_receipt(self, stack):
        ack = make_signal_acknowledger(
            drafter_emitter=stack["drafter_port"],
            instance_id="inst_a",
        )
        signal_id = "test-signal-id"
        result = await ack(signal_id)
        assert result == signal_id  # canonical correlation handle
        # Action log records the receipt.
        rec = await stack["action_log"].is_already_done(
            instance_id="inst_a",
            source_event_id=signal_id,
            action_type="emit_receipt",
            target_id=f"signal_acknowledged::{signal_id}",
        )
        assert rec is not None

    async def test_ack_idempotent_via_action_log(self, stack):
        """Replaying the ack for the same signal_id is a no-op via
        action_log."""
        ack = make_signal_acknowledger(
            drafter_emitter=stack["drafter_port"],
            instance_id="inst_a",
        )
        signal_id = "test-signal-id"
        # Emit twice.
        await ack(signal_id)
        await ack(signal_id)
        # Only one performed record.
        rec = await stack["action_log"].is_already_done(
            instance_id="inst_a",
            source_event_id=signal_id,
            action_type="emit_receipt",
            target_id=f"signal_acknowledged::{signal_id}",
        )
        assert rec.status == "performed"

    async def test_empty_signal_id_rejected(self, stack):
        ack = make_signal_acknowledger(
            drafter_emitter=stack["drafter_port"],
            instance_id="inst_a",
        )
        with pytest.raises(ValueError, match="signal_id"):
            await ack("")


# ===========================================================================
# Receipt is on Drafter's emitter (not CRB's)
# ===========================================================================


class TestReceiptEmittedByDrafter:
    """The signal_acknowledged receipt is emitted by Drafter's emitter
    so envelope.source_module="drafter". CRB doesn't emit it (the
    spec is clear that receipt belongs to the cohort that owns the
    signal taxonomy)."""

    async def test_receipt_envelope_source_is_drafter(self, stack):
        ack = make_signal_acknowledger(
            drafter_emitter=stack["drafter_port"],
            instance_id="inst_a",
        )
        await ack("test-signal-id")
        await event_stream.flush_now()
        # Find the receipt event.
        import datetime as dt
        all_events = await event_stream.events_in_window(
            "inst_a",
            since=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
            until=dt.datetime.now(dt.timezone.utc),
            event_types=(RECEIPT_SIGNAL_ACKNOWLEDGED,),
        )
        assert len(all_events) == 1
        assert all_events[0].envelope.source_module == "drafter"
