import logging
from abc import ABC, abstractmethod

from kernos.messages.models import NormalizedMessage

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    @abstractmethod
    def inbound(self, raw_request: dict) -> NormalizedMessage:
        """Translate a platform-native inbound request to a NormalizedMessage."""
        ...

    @abstractmethod
    def outbound(self, response: str, original_message: NormalizedMessage) -> object:
        """Translate a response string to a platform-native response object."""
        ...

    async def send_outbound(self, tenant_id: str, channel_target: str, message: str) -> bool:
        """Send an unprompted message to the user. Returns True if sent, False on failure.

        Default: not supported. Adapters that support outbound override this.
        """
        return False

    @property
    def can_send_outbound(self) -> bool:
        """Whether this adapter supports sending unprompted messages."""
        return False
