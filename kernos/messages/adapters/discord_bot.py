import logging
import os

import discord

from kernos.messages.adapters.base import BaseAdapter
from kernos.messages.models import AuthLevel, NormalizedMessage

# This adapter knows about NormalizedMessage, BaseAdapter, and discord.Message.
# It knows nothing about the handler or the kernel.

logger = logging.getLogger(__name__)

DISCORD_CAPABILITIES = ["text", "embeds", "attachments", "reactions"]


class DiscordAdapter(BaseAdapter):
    """
    Translates between Discord Message objects and NormalizedMessage.

    Knows about Discord. Knows nothing about the handler or the kernel.

    Discord accounts are authenticated sessions — per Blueprint, this is
    medium-high auth confidence, so matching the owner ID grants owner_verified
    (unlike SMS which only gets owner_unverified from a phone number match).
    """

    def __init__(self) -> None:
        self._owner_id = os.getenv("DISCORD_OWNER_ID", "")
        self._tenant_id = os.getenv("OWNER_PHONE_NUMBER", "")

    def inbound(self, raw_request: discord.Message) -> NormalizedMessage:  # type: ignore[override]
        """Translate a discord.Message into a NormalizedMessage."""
        author_id = str(raw_request.author.id)

        # Discord accounts are authenticated sessions — ID match grants owner_verified.
        auth_level = (
            AuthLevel.owner_verified
            if self._owner_id and author_id == self._owner_id
            else AuthLevel.unknown
        )

        # Include guild context when message is in a server; None for DMs.
        if raw_request.guild is not None:
            context: dict | None = {
                "guild_id": str(raw_request.guild.id),
                "channel_name": raw_request.channel.name,
            }
        else:
            context = None

        return NormalizedMessage(
            content=raw_request.content,
            sender=author_id,
            sender_auth_level=auth_level,
            platform="discord",
            platform_capabilities=DISCORD_CAPABILITIES,
            # Discord has real channels/threads — use channel ID for conversation continuity.
            conversation_id=str(raw_request.channel.id),
            timestamp=raw_request.created_at,
            # Phase 1A: single-tenant. tenant_id is the owner's phone number.
            tenant_id=self._tenant_id,
            context=context,
        )

    def outbound(self, response: str, original_message: NormalizedMessage) -> str:
        """
        Return the response string unchanged.

        Discord's API handles sending via message.channel.send(). No length
        concerns for Phase 1A — Discord allows 2000 chars and Claude's
        SMS-optimised responses are well under that.
        """
        return response
