from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AuthLevel(str, Enum):
    owner_verified = "owner_verified"
    owner_unverified = "owner_unverified"
    trusted_contact = "trusted_contact"
    unknown = "unknown"


@dataclass
class NormalizedMessage:
    content: str
    sender: str
    sender_auth_level: AuthLevel
    platform: str  # "sms", "discord", "telegram", "voice", "app"
    platform_capabilities: list[str]
    conversation_id: str
    timestamp: datetime
    tenant_id: str
    context: Optional[dict] = None
