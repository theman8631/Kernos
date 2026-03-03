from abc import ABC, abstractmethod

from kernos.messages.models import NormalizedMessage


class BaseAdapter(ABC):
    @abstractmethod
    def inbound(self, raw_request: dict) -> NormalizedMessage:
        """Translate a platform-native inbound request to a NormalizedMessage."""
        ...

    @abstractmethod
    def outbound(self, response: str, original_message: NormalizedMessage) -> object:
        """Translate a response string to a platform-native response object."""
        ...
