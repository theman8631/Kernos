"""Telegram Bot adapter — translates between Telegram Updates and NormalizedMessage.

Knows about Telegram. Knows nothing about the handler or the kernel.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any

from kernos.messages.adapters.base import BaseAdapter
from kernos.messages.models import AuthLevel, NormalizedMessage

logger = logging.getLogger(__name__)

TELEGRAM_CAPABILITIES = ["text"]
TELEGRAM_MAX_LENGTH = 4096


class TelegramAdapter(BaseAdapter):
    """Translates between Telegram Update dicts and NormalizedMessage.

    Telegram accounts are phone-verified sessions. All senders start as
    AuthLevel.unknown — member resolution in handler decides identity.
    """

    def __init__(self) -> None:
        self._instance_id = os.getenv("KERNOS_INSTANCE_ID") or os.getenv("OWNER_PHONE_NUMBER", "")
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._http: Any = None

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    def inbound(self, raw_request: dict) -> NormalizedMessage:  # type: ignore[override]
        """Translate a Telegram Update dict into a NormalizedMessage."""
        msg = raw_request.get("message", {})
        from_user = msg.get("from", {})

        sender = str(from_user.get("id", ""))
        content = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        timestamp = datetime.fromtimestamp(msg.get("date", 0), tz=timezone.utc)

        return NormalizedMessage(
            content=content,
            sender=sender,
            sender_auth_level=AuthLevel.unknown,
            platform="telegram",
            platform_capabilities=TELEGRAM_CAPABILITIES,
            conversation_id=chat_id,
            timestamp=timestamp,
            instance_id=self._instance_id,
        )

    def outbound(self, response: str, original_message: NormalizedMessage) -> str:
        return response

    async def send_outbound(self, instance_id: str, channel_target: str, message: str) -> int:
        """Send a message to a Telegram chat. Returns message_id or 0."""
        if not self._bot_token or not channel_target:
            return 0
        http = await self._ensure_http()
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        chunks = self._chunk(message)
        last_id = 0
        try:
            for chunk in chunks:
                resp = await http.post(url, json={
                    "chat_id": channel_target,
                    "text": chunk,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    last_id = data.get("result", {}).get("message_id", 0)
                else:
                    logger.warning("TELEGRAM_SEND: status=%d body=%s",
                        resp.status_code, resp.text[:200])
            logger.info("OUTBOUND: channel=telegram target=%s length=%d chunks=%d success=True",
                channel_target, len(message), len(chunks))
            return last_id
        except Exception as exc:
            logger.warning("OUTBOUND: channel=telegram target=%s success=False error=%s",
                channel_target, exc)
            return 0

    @classmethod
    def _chunk(cls, text: str) -> list[str]:
        """Split text into chunks that fit Telegram's 4096-char limit."""
        if len(text) <= TELEGRAM_MAX_LENGTH:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= TELEGRAM_MAX_LENGTH:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
            if cut <= 0:
                cut = TELEGRAM_MAX_LENGTH
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    @property
    def can_send_outbound(self) -> bool:
        return bool(self._bot_token)
