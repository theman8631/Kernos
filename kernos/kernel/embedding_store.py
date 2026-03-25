"""Separate storage for embedding vectors.

Stored at {data_dir}/{tenant_id}/state/embeddings.json
Maps entry_id → list[float].

Kept separate from knowledge.json to avoid bloating it with large float arrays.
"""
import json
import logging
from pathlib import Path

from filelock import FileLock

from kernos.utils import _safe_name

logger = logging.getLogger(__name__)


class JsonEmbeddingStore:
    """JSON-on-disk store for embedding vectors, partitioned by tenant."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _path(self, tenant_id: str) -> Path:
        return self._data_dir / _safe_name(tenant_id) / "state" / "embeddings.json"

    def _read(self, tenant_id: str) -> dict[str, list[float]]:
        path = self._path(tenant_id)
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, tenant_id: str, data: dict[str, list[float]]) -> None:
        path = self._path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    async def save(self, tenant_id: str, entry_id: str, embedding: list[float]) -> None:
        """Store an embedding vector for the given entry ID."""
        data = self._read(tenant_id)
        data[entry_id] = embedding
        self._write(tenant_id, data)

    async def get(self, tenant_id: str, entry_id: str) -> list[float] | None:
        """Retrieve embedding for entry_id, or None if not found."""
        data = self._read(tenant_id)
        return data.get(entry_id)

    async def get_batch(
        self, tenant_id: str, entry_ids: list[str]
    ) -> dict[str, list[float]]:
        """Retrieve embeddings for multiple entry IDs in one read.

        Returns dict mapping entry_id → embedding (only present entries included).
        """
        data = self._read(tenant_id)
        return {eid: data[eid] for eid in entry_ids if eid in data}

    async def delete(self, tenant_id: str, entry_id: str) -> None:
        """Remove an embedding entry (non-destructive: just removes from index)."""
        data = self._read(tenant_id)
        if entry_id in data:
            del data[entry_id]
            self._write(tenant_id, data)
