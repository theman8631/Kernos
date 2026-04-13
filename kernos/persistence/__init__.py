"""Persistence module for KERNOS.

The kernel remembers. The agent thinks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.persistence.base import AuditStore, ConversationStore, InstanceStore

if TYPE_CHECKING:
    from kernos.messages.models import NormalizedMessage


def derive_instance_id(message: NormalizedMessage) -> str:
    """Derive instance_id from a NormalizedMessage.

    If the adapter already set instance_id (e.g., from KERNOS_INSTANCE_ID),
    use it. Otherwise derive from platform:sender.
    """
    if message.instance_id:
        return message.instance_id
    return f"{message.platform}:{message.sender}"


__all__ = [
    "ConversationStore",
    "InstanceStore",
    "AuditStore",
    "derive_instance_id",
]
