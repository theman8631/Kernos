"""Drafter receipt taxonomy tests (DRAFTER C3, AC #19, #20, #29)."""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.receipts import (
    DEFAULT_PAUSED_STATES,
    DEFAULT_TIMEOUT_SECONDS,
    RECEIPT_DRAFT_UPDATED,
    RECEIPT_DRY_RUN_COMPLETED,
    RECEIPT_SIGNAL_ACKNOWLEDGED,
    RECEIPT_SIGNAL_EMITTED,
    RECEIPT_TYPES,
    ReceiptTimeoutConfig,
    build_draft_updated_payload,
    build_dry_run_completed_payload,
    build_signal_acknowledged_payload,
    build_signal_emitted_payload,
)


class TestReceiptSurface:
    def test_five_receipt_types_pinned(self):
        # v1.1: gained drafter.receipt.feedback_received.
        assert RECEIPT_TYPES == frozenset({
            "drafter.receipt.signal_emitted",
            "drafter.receipt.signal_acknowledged",
            "drafter.receipt.draft_updated",
            "drafter.receipt.dry_run_completed",
            "drafter.receipt.feedback_received",
        })


class TestReceiptTimeoutConfig:
    def test_defaults(self):
        cfg = ReceiptTimeoutConfig()
        assert cfg.threshold_seconds == DEFAULT_TIMEOUT_SECONDS
        assert cfg.enabled is True
        assert cfg.paused_states == DEFAULT_PAUSED_STATES

    def test_default_paused_states_includes_degraded_startup(self):
        """AC #29: paused_states includes 'degraded_startup'."""
        cfg = ReceiptTimeoutConfig()
        assert "degraded_startup" in cfg.paused_states
        assert "soak_paused" in cfg.paused_states
        assert "manual_pause" in cfg.paused_states

    def test_is_paused_true_for_listed_state(self):
        cfg = ReceiptTimeoutConfig()
        assert cfg.is_paused(principal_state="degraded_startup") is True
        assert cfg.is_paused(principal_state="soak_paused") is True

    def test_is_paused_false_for_active_state(self):
        cfg = ReceiptTimeoutConfig()
        assert cfg.is_paused(principal_state="active") is False
        assert cfg.is_paused(principal_state=None) is False

    def test_threshold_must_be_positive(self):
        with pytest.raises(ValueError):
            ReceiptTimeoutConfig(threshold_seconds=0)
        with pytest.raises(ValueError):
            ReceiptTimeoutConfig(threshold_seconds=-1)

    def test_custom_paused_states_per_instance(self):
        cfg = ReceiptTimeoutConfig(
            paused_states=frozenset({"custom_pause"}),
        )
        assert cfg.is_paused(principal_state="custom_pause") is True
        # Defaults are NOT included when caller overrides.
        assert cfg.is_paused(principal_state="degraded_startup") is False

    def test_disabled_config_pin(self):
        cfg = ReceiptTimeoutConfig(enabled=False)
        # Caller (cohort) decides what to do with enabled=False; the
        # config just exposes the flag.
        assert cfg.enabled is False


class TestPayloadBuilders:
    def test_signal_emitted_payload(self):
        payload = build_signal_emitted_payload(
            signal_type="drafter.signal.draft_ready",
            signal_id="sig-1",
        )
        assert payload["signal_type"] == "drafter.signal.draft_ready"
        assert payload["signal_id"] == "sig-1"
        assert payload["target_cohort"] == "principal"
        assert "emitted_at" in payload

    def test_signal_acknowledged_payload(self):
        payload = build_signal_acknowledged_payload(signal_id="sig-1")
        assert payload["signal_id"] == "sig-1"
        assert "acknowledged_at" in payload

    def test_draft_updated_payload(self):
        payload = build_draft_updated_payload(
            draft_id="d-1", instance_id="inst_a", version_after=2,
        )
        assert payload["version_after"] == 2

    def test_dry_run_completed_payload(self):
        payload = build_dry_run_completed_payload(
            draft_id="d-1", descriptor_hash="abc",
            valid=True, issue_count=0, capability_gap_count=0,
        )
        assert payload["valid"] is True
        assert payload["descriptor_hash"] == "abc"

    def test_payload_missing_required_raises(self):
        with pytest.raises(ValueError):
            build_signal_emitted_payload(signal_type="", signal_id="sig-1")
        with pytest.raises(ValueError):
            build_dry_run_completed_payload(
                draft_id="", descriptor_hash="abc",
                valid=True, issue_count=0, capability_gap_count=0,
            )
