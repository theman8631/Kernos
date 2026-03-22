"""Persistence module for KERNOS.

The kernel remembers. The agent thinks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.persistence.base import AuditStore, ConversationStore, TenantStore

if TYPE_CHECKING:
    from kernos.messages.models import NormalizedMessage


def derive_tenant_id(message: NormalizedMessage) -> str:
    """Derive tenant_id from a NormalizedMessage.

    If the adapter already set tenant_id (e.g., from KERNOS_INSTANCE_ID),
    use it. Otherwise derive from platform:sender.
    """
    if message.tenant_id:
        return message.tenant_id
    return f"{message.platform}:{message.sender}"


__all__ = [
    "ConversationStore",
    "TenantStore",
    "AuditStore",
    "derive_tenant_id",
]
