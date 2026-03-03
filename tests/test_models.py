from datetime import datetime, timezone

from kernos.messages.models import AuthLevel, NormalizedMessage


def test_normalized_message_creation():
    msg = NormalizedMessage(
        content="Hello",
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform="sms",
        platform_capabilities=["text", "mms"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        tenant_id="+15555550100",
    )
    assert msg.content == "Hello"
    assert msg.sender == "+15555550100"
    assert msg.platform == "sms"
    assert msg.tenant_id == "+15555550100"
    assert msg.context is None


def test_normalized_message_tenant_id_required():
    """tenant_id is a required field — omitting it must raise TypeError."""
    import pytest

    with pytest.raises(TypeError):
        NormalizedMessage(  # type: ignore[call-arg]
            content="Hello",
            sender="+15555550100",
            sender_auth_level=AuthLevel.unknown,
            platform="sms",
            platform_capabilities=["text"],
            conversation_id="+15555550100",
            timestamp=datetime.now(timezone.utc),
            # tenant_id omitted
        )


def test_normalized_message_with_context():
    msg = NormalizedMessage(
        content="Test",
        sender="+15555550100",
        sender_auth_level=AuthLevel.unknown,
        platform="sms",
        platform_capabilities=["text"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        tenant_id="+15555550100",
        context={"key": "value"},
    )
    assert msg.context == {"key": "value"}


def test_auth_level_string_values():
    assert AuthLevel.owner_verified.value == "owner_verified"
    assert AuthLevel.owner_unverified.value == "owner_unverified"
    assert AuthLevel.trusted_contact.value == "trusted_contact"
    assert AuthLevel.unknown.value == "unknown"
