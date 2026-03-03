import os
from datetime import datetime, timezone

from twilio.twiml.messaging_response import MessagingResponse

from kernos.messages.adapters.base import BaseAdapter
from kernos.messages.models import AuthLevel, NormalizedMessage

# SMS supports text and MMS (images/media); list both per Blueprint capability model.
SMS_CAPABILITIES = ["text", "mms"]

_SMS_LIMIT = 1600
_SMS_CHUNK = 1550
_MORE_SUFFIX = " [...] Reply MORE for the rest."


class TwilioSMSAdapter(BaseAdapter):
    """
    Translates between Twilio SMS webhook payloads and NormalizedMessage.

    Knows about Twilio. Knows nothing about the handler or the kernel.

    Twilio webhook fields used:
        From    — sender's E.164 phone number
        To      — our Twilio number (used for tenant routing once multi-tenant)
        Body    — message text
        SmsSid  — per-message Twilio identifier
    """

    def __init__(self) -> None:
        self._owner_phone = os.getenv("OWNER_PHONE_NUMBER", "")
        self._overflow: dict[str, str] = {}  # conversation_id → remaining text

    def inbound(self, raw_request: dict) -> NormalizedMessage:
        """Translate a Twilio webhook form payload into a NormalizedMessage."""
        sender = raw_request.get("From", "")
        body = raw_request.get("Body", "").strip()

        # Phone number is identification, not authentication — per Blueprint.
        # A match gives owner_unverified, not owner_verified.
        auth_level = (
            AuthLevel.owner_unverified
            if sender and sender == self._owner_phone
            else AuthLevel.unknown
        )

        # Phase 1A: single-tenant. tenant_id is the owner's phone number.
        # Phase 1B will replace this with a database lookup keyed to tenant record.
        tenant_id = self._owner_phone

        # conversation_id is per-sender — SMS has no thread concept beyond who's talking.
        conversation_id = sender

        return NormalizedMessage(
            content=body,
            sender=sender,
            sender_auth_level=auth_level,
            platform="sms",
            platform_capabilities=SMS_CAPABILITIES,
            conversation_id=conversation_id,
            timestamp=datetime.now(timezone.utc),
            tenant_id=tenant_id,
        )

    def outbound(self, response: str, original_message: NormalizedMessage) -> str:
        """Return a TwiML string ready to send as an HTTP response to Twilio."""
        conversation_id = original_message.conversation_id

        # If this was a MORE request and overflow exists, serve the next chunk
        # instead of the handler's response (which was generated unnecessarily).
        if original_message.content.strip().upper() == "MORE" and conversation_id in self._overflow:
            remaining = self._overflow.pop(conversation_id)
            if len(remaining) > _SMS_LIMIT:
                text = remaining[:_SMS_CHUNK] + _MORE_SUFFIX
                self._overflow[conversation_id] = remaining[_SMS_CHUNK:]
            else:
                text = remaining
        elif len(response) > _SMS_LIMIT:
            text = response[:_SMS_CHUNK] + _MORE_SUFFIX
            self._overflow[conversation_id] = response[_SMS_CHUNK:]
        else:
            text = response

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)
