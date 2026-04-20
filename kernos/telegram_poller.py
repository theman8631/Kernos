"""Telegram Bot API long poller — polls getUpdates for incoming messages.

Same structural pattern as sms_poller.py. Telegram's getUpdates supports
native long polling — the server holds the connection for up to N seconds
and returns immediately when a message arrives.
"""
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TelegramPoller:
    """Polls Telegram Bot API for incoming messages via getUpdates long polling."""

    def __init__(
        self,
        adapter: Any,
        handler: Any,
        bot_token: str,
        poll_timeout: int = 30,
    ) -> None:
        self._adapter = adapter
        self._handler = handler
        self._bot_token = bot_token
        self._poll_timeout = poll_timeout
        self._last_update_id = 0
        self._task: asyncio.Task | None = None
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._http: Any = None

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=self._poll_timeout + 10)
        return self._http

    async def discover_identity(self) -> dict:
        """Call Telegram's getMe to learn the bot's username and display name.

        Returns dict with bot_username, bot_name. Empty dict on failure.
        """
        try:
            http = await self._ensure_http()
            resp = await http.get(f"{self._base_url}/getMe")
            if resp.status_code == 200:
                data = resp.json().get("result", {})
                identity = {
                    "bot_username": data.get("username", ""),
                    "bot_name": data.get("first_name", ""),
                }
                logger.info("TELEGRAM_IDENTITY: @%s (%s)", identity["bot_username"], identity["bot_name"])
                return identity
            logger.warning("TELEGRAM_IDENTITY: getMe returned %d", resp.status_code)
        except Exception as exc:
            logger.warning("TELEGRAM_IDENTITY: getMe failed: %s", exc)
        return {}

    async def start(self) -> None:
        # Idempotent: if a poll task is already running, return without
        # starting a second one. Two concurrent pollers on the same bot
        # token cause Telegram to return HTTP 409 Conflict on every
        # getUpdates call, which spams the log every ~3s.
        if self._task is not None and not self._task.done():
            logger.info("TELEGRAM_POLL: start() called but poller already running; ignoring")
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        conflict_streak = 0
        while True:
            try:
                await self._check_updates()
                conflict_streak = 0
            except asyncio.CancelledError:
                raise
            except _Conflict409 as exc:
                # Another poller owns this bot token. Warn once per streak,
                # back off, and try again — typically the other process is
                # about to exit or was already stale.
                if conflict_streak == 0:
                    logger.warning(
                        "TELEGRAM_POLL: HTTP 409 — another getUpdates caller "
                        "owns this bot token. Backing off; will keep retrying."
                    )
                conflict_streak += 1
                # 5s, 10s, 20s, 40s, cap 60s.
                backoff = min(5 * (2 ** min(conflict_streak - 1, 4)), 60)
                await asyncio.sleep(backoff)
            except Exception as exc:
                _msg = str(exc).lower()
                # Auth failure — stop cleanly, don't retry
                if "401" in _msg or "unauthorized" in _msg:
                    logger.error("TELEGRAM_POLL: invalid bot token — stopping poller")
                    break
                logger.warning("TELEGRAM_POLL: error: %s", exc)
                await asyncio.sleep(5)


class _Conflict409(Exception):
    """Raised when Telegram returns HTTP 409 (another getUpdates owner)."""

    async def _check_updates(self) -> None:
        """GET /getUpdates with long polling."""
        http = await self._ensure_http()
        params = {
            "offset": self._last_update_id + 1,
            "timeout": self._poll_timeout,
            "allowed_updates": '["message"]',
        }

        resp = await http.get(f"{self._base_url}/getUpdates", params=params)

        if resp.status_code == 401:
            raise Exception("401 Unauthorized — invalid bot token")

        if resp.status_code == 409:
            # Another poller owns this token — short-circuit to the
            # poll-loop's backoff handler via a typed exception rather
            # than flooding the log with identical warnings every 3s.
            raise _Conflict409(resp.text[:200])

        if resp.status_code != 200:
            logger.warning("TELEGRAM_POLL: HTTP %d: %s", resp.status_code, resp.text[:200])
            return

        data = resp.json()
        if not data.get("ok"):
            logger.warning("TELEGRAM_POLL: API error: %s", data.get("description", "unknown"))
            return

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            if update_id > self._last_update_id:
                self._last_update_id = update_id

            message = update.get("message")
            if not message:
                continue

            # V1: text only
            if "text" not in message:
                _type = next(
                    (k for k in ("sticker", "photo", "voice", "document", "video", "audio")
                     if k in message), "unknown")
                logger.info("TELEGRAM_POLL: unsupported message type=%s", _type)
                continue

            chat_id = str(message.get("chat", {}).get("id", ""))
            from_user = message.get("from", {})
            sender_id = str(from_user.get("id", ""))

            logger.info("TELEGRAM_MSG: sender=%s chat=%s text=%s",
                sender_id, chat_id, message.get("text", "")[:60])

            # Build NormalizedMessage via adapter
            normalized = self._adapter.inbound(update)

            # Process through handler
            try:
                response_text = await self._handler.process(normalized)
            except Exception as exc:
                logger.error("TELEGRAM_POLL: handler failed: %s", exc)
                response_text = "Something went wrong — try again in a moment."

            # Send reply
            if response_text:
                await self._adapter.send_outbound(
                    instance_id=normalized.instance_id,
                    channel_target=chat_id,
                    message=response_text,
                )
