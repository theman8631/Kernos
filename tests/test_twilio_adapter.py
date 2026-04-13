import os
from unittest.mock import patch

import pytest

from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
from kernos.messages.models import AuthLevel

OWNER_PHONE = "+15555550100"
OTHER_PHONE = "+19995550100"

SAMPLE_FORM = {
    "From": OWNER_PHONE,
    "To": "+12345678901",
    "Body": "Hello Kernos",
    "SmsSid": "SM123",
}


@pytest.fixture
def adapter():
    with patch.dict(os.environ, {"OWNER_PHONE_NUMBER": OWNER_PHONE}):
        return TwilioSMSAdapter()


# --- Inbound ---


def test_inbound_owner_gets_unverified_auth(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    assert msg.sender_auth_level == AuthLevel.owner_unverified


def test_inbound_unknown_sender_gets_unknown_auth(adapter):
    data = {**SAMPLE_FORM, "From": OTHER_PHONE}
    msg = adapter.inbound(data)
    assert msg.sender_auth_level == AuthLevel.unknown


def test_inbound_fields(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    assert msg.content == "Hello Kernos"
    assert msg.sender == OWNER_PHONE
    assert msg.platform == "sms"
    assert "text" in msg.platform_capabilities
    assert "mms" in msg.platform_capabilities
    assert msg.conversation_id == OWNER_PHONE


def test_inbound_instance_id_is_owner_phone(adapter):
    """Phase 1A: instance_id is always OWNER_PHONE_NUMBER, not the sender."""
    msg = adapter.inbound(SAMPLE_FORM)
    assert msg.instance_id == OWNER_PHONE


def test_inbound_unknown_sender_instance_id_is_still_owner_phone(adapter):
    """Even unknown senders resolve to the single owner tenant in Phase 1A."""
    data = {**SAMPLE_FORM, "From": OTHER_PHONE}
    msg = adapter.inbound(data)
    assert msg.instance_id == OWNER_PHONE


# --- Outbound: basic TwiML ---


def test_outbound_returns_twiml(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    twiml = adapter.outbound("Hello back!", msg)
    assert "<Response>" in twiml
    assert "<Message>" in twiml
    assert "Hello back!" in twiml


def test_outbound_short_response_no_truncation(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    twiml = adapter.outbound("Short response", msg)
    assert "Short response" in twiml
    assert "MORE" not in twiml


def test_outbound_exactly_1600_chars_no_truncation(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    response = "A" * 1600
    twiml = adapter.outbound(response, msg)
    assert "MORE" not in twiml
    assert "A" * 1600 in twiml


# --- Outbound: SMS overflow / truncation ---


def test_outbound_over_1600_triggers_truncation(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    response = "B" * 1700
    twiml = adapter.outbound(response, msg)
    assert "MORE" in twiml
    assert len("B" * 1700) > 1600  # sanity


def test_overflow_stored_after_truncation(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    response = "C" * 1700
    adapter.outbound(response, msg)
    assert msg.conversation_id in adapter._overflow
    assert adapter._overflow[msg.conversation_id] == "C" * 150  # 1700 - 1550


def test_more_continuation_returns_overflow(adapter):
    # First message triggers overflow
    msg = adapter.inbound(SAMPLE_FORM)
    adapter.outbound("D" * 1700, msg)

    # Simulate sender replying MORE
    more_msg = adapter.inbound({**SAMPLE_FORM, "Body": "MORE"})
    twiml = adapter.outbound("Claude reply to MORE (should be ignored)", more_msg)

    # Should return the overflow (150 Ds), not the Claude reply
    assert "D" * 150 in twiml
    assert "Claude reply to MORE" not in twiml


def test_more_case_insensitive(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    adapter.outbound("E" * 1700, msg)

    more_msg = adapter.inbound({**SAMPLE_FORM, "Body": "more"})
    twiml = adapter.outbound("ignored", more_msg)
    assert "E" * 150 in twiml


def test_more_without_overflow_uses_handler_response(adapter):
    """MORE with no stored overflow just returns the handler's response."""
    more_msg = adapter.inbound({**SAMPLE_FORM, "Body": "MORE"})
    twiml = adapter.outbound("Normal Claude response", more_msg)
    assert "Normal Claude response" in twiml


def test_more_clears_overflow(adapter):
    msg = adapter.inbound(SAMPLE_FORM)
    adapter.outbound("F" * 1700, msg)

    more_msg = adapter.inbound({**SAMPLE_FORM, "Body": "MORE"})
    adapter.outbound("ignored", more_msg)

    # Overflow should be consumed
    assert msg.conversation_id not in adapter._overflow
