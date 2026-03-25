"""Channel Registry — tracks communication channels and their status.

Each channel represents a way to reach the user (Discord, SMS, CLI).
Channels are adapters with outbound capability metadata.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ChannelInfo:
    """A registered communication channel."""

    name: str                    # "discord", "sms", "cli"
    display_name: str            # "Discord", "Twilio SMS", "CLI Terminal"
    status: str                  # "connected", "available", "disabled", "error"
    source: str                  # "default" or "user"
    can_send_outbound: bool      # Whether this channel can push messages
    channel_target: str          # Where to send: channel ID, phone number, etc.
    platform: str                # Maps to NormalizedMessage.platform field


class ChannelRegistry:
    """Tracks available communication channels for the instance."""

    def __init__(self) -> None:
        self._channels: dict[str, ChannelInfo] = {}

    def register(self, channel: ChannelInfo) -> None:
        self._channels[channel.name] = channel

    def get(self, name: str) -> ChannelInfo | None:
        return self._channels.get(name)

    def get_all(self) -> list[ChannelInfo]:
        return list(self._channels.values())

    def get_connected(self) -> list[ChannelInfo]:
        return [c for c in self._channels.values() if c.status == "connected"]

    def get_outbound_capable(self) -> list[ChannelInfo]:
        return [
            c for c in self._channels.values()
            if c.status == "connected" and c.can_send_outbound
        ]

    def disable(self, name: str) -> bool:
        ch = self._channels.get(name)
        if not ch or ch.status != "connected":
            return False
        ch.status = "disabled"
        logger.info("CAP_WRITE: name=%s action=DISABLE source=channel_registry", name)
        return True

    def enable(self, name: str) -> bool:
        ch = self._channels.get(name)
        if not ch or ch.status != "disabled":
            return False
        ch.status = "connected"
        logger.info("CAP_WRITE: name=%s action=ENABLE source=channel_registry", name)
        return True

    def update_target(self, name: str, target: str) -> None:
        """Update the channel_target (e.g., most recent Discord channel ID)."""
        ch = self._channels.get(name)
        if ch:
            ch.channel_target = target


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

MANAGE_CHANNELS_TOOL = {
    "name": "manage_channels",
    "description": (
        "Manage communication channels — list connected channels, enable, disable. "
        "Use 'list' to see all channels and their status. "
        "Channels determine how you can reach the user and how they reach you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "enable", "disable"],
                "description": "The action to perform.",
            },
            "channel": {
                "type": "string",
                "description": "The channel name (required for enable/disable).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def handle_manage_channels(registry: "ChannelRegistry", action: str, channel: str = "") -> str:
    """Handle the manage_channels kernel tool."""
    if action == "list":
        channels = registry.get_all()
        if not channels:
            return "No communication channels registered."
        lines = ["**Communication Channels:**\n"]
        for ch in channels:
            outbound = "can push" if ch.can_send_outbound else "receive only"
            target = f" → {ch.channel_target}" if ch.channel_target else ""
            lines.append(f"- **{ch.display_name}** [{ch.status}] ({outbound}){target}")
        return "\n".join(lines)

    if action == "enable":
        if not channel:
            return "Error: 'channel' is required for enable."
        if registry.enable(channel):
            return f"Enabled channel '{channel}'."
        ch = registry.get(channel)
        if not ch:
            return f"Error: Channel '{channel}' not found."
        return f"Cannot enable '{channel}' — current status is '{ch.status}'."

    if action == "disable":
        if not channel:
            return "Error: 'channel' is required for disable."
        if registry.disable(channel):
            return f"Disabled channel '{channel}'. It will not send or receive messages."
        ch = registry.get(channel)
        if not ch:
            return f"Error: Channel '{channel}' not found."
        return f"Cannot disable '{channel}' — current status is '{ch.status}'."

    return f"Unknown action: '{action}'. Use list, enable, or disable."


# ---------------------------------------------------------------------------
# Channel alias resolver
# ---------------------------------------------------------------------------

_CHANNEL_ALIASES: dict[str, str] = {
    # SMS aliases
    "sms": "sms",
    "text": "sms",
    "phone": "sms",
    "my phone": "sms",
    "txt": "sms",
    # Discord aliases
    "discord": "discord",
    "chat": "discord",
    "over chat": "discord",
    # Email (future — when Gmail is connected)
    "email": "email",
    "gmail": "email",
    "mail": "email",
}


def resolve_channel_alias(name: str) -> str:
    """Resolve a user-friendly channel name to the canonical name.

    Returns the canonical name, or the input unchanged if no alias matches.
    Deterministic — no LLM call.
    """
    return _CHANNEL_ALIASES.get(name.strip().lower(), name.strip().lower())


# ---------------------------------------------------------------------------
# send_to_channel tool definition
# ---------------------------------------------------------------------------

SEND_TO_CHANNEL_TOOL = {
    "name": "send_to_channel",
    "description": (
        "Send a message to the user on a specific channel. "
        "Use when the user asks to receive something on a different channel "
        "than the current one — e.g., 'send that to my Discord' or "
        "'text me the summary'. Use manage_channels list to see available channels."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": (
                    "Target channel name: 'discord', 'sms'. "
                    "Common aliases also work: 'text'/'phone' → sms, "
                    "'chat' → discord."
                ),
            },
            "message": {
                "type": "string",
                "description": "The message content to deliver.",
            },
        },
        "required": ["channel", "message"],
        "additionalProperties": False,
    },
}
