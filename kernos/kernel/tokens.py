"""Token Adapter — provider-agnostic token counting for the compaction system.

The compaction system never references provider-specific API field names directly.
All token measurement flows through this adapter.
"""
import logging
import math
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class TokenAdapter(ABC):
    """Abstract token counting adapter."""

    @abstractmethod
    async def count_tokens(self, text: str) -> int:
        """Count the number of tokens in the given text."""
        ...


class AnthropicTokenAdapter(TokenAdapter):
    """Wraps Anthropic's free count_tokens endpoint.

    Falls back to EstimateTokenAdapter on any failure — token counting
    never breaks the user's message flow.
    """

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self.model = model
        self._client = None
        self._fallback = EstimateTokenAdapter()

    def _ensure_client(self) -> None:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)

    async def count_tokens(self, text: str) -> int:
        try:
            self._ensure_client()
            response = self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": text}],
            )
            return response.input_tokens
        except Exception as exc:
            logger.warning("Anthropic token count failed, using estimate: %s", exc)
            return await self._fallback.count_tokens(text)


class EstimateTokenAdapter(TokenAdapter):
    """Character-based estimation with 20% safety buffer.

    Biases toward early compaction, which is the safe direction.
    """

    async def count_tokens(self, text: str) -> int:
        return math.ceil(len(text) / 4 * 1.2)
