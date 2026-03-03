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

    Phase 1A: simple platform:sender mapping.
    Phase 2+: proper identity resolution across platforms.
    """
    return f"{message.platform}:{message.sender}"


__all__ = [
    "ConversationStore",
    "TenantStore",
    "AuditStore",
    "derive_tenant_id",
]
