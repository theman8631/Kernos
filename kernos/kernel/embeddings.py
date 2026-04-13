"""Embedding generation for entity resolution and fact deduplication.

Uses Voyage AI's voyage-3-lite model (Anthropic-adjacent, purpose-built for retrieval).
Embeddings are computed on write and stored in a separate embeddings.json per-instance.
At personal scale (~hundreds of entries), cost is negligible (~$0.00001 per embedding).
"""
import asyncio
import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generate text embeddings via Voyage AI (voyage-3-lite)."""

    MODEL = "voyage-3-lite"

    def __init__(self, api_key: str) -> None:
        import voyageai
        self._client = voyageai.Client(api_key=api_key)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string.

        Wraps the sync Voyage client in asyncio.to_thread so the event loop
        is not blocked during the HTTP call.
        """
        result = await asyncio.to_thread(
            self._client.embed, [text], model=self.MODEL
        )
        return result.embeddings[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in one API call.

        Returns list of embedding vectors in the same order as input.
        """
        if not texts:
            return []
        result = await asyncio.to_thread(
            self._client.embed, texts, model=self.MODEL
        )
        return result.embeddings


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors.

    Returns float in range [-1.0, 1.0], typically [0.0, 1.0] for text embeddings.
    Returns 0.0 for empty or zero-magnitude vectors.
    """
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)
