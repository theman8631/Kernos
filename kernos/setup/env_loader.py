"""Load secrets from the active storage backend into ``os.environ``.

Called at startup, after the binary health check passes, so the existing
``build_chains_from_env()`` path finds the credentials it expects.

**Zero-LLM-call.** File / keychain reads only.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from kernos.setup.chain_config_io import configured_providers, load_chain_config
from kernos.setup.provider_registry import get_provider
from kernos.setup.storage_backend import active_backend

logger = logging.getLogger(__name__)


def load_secrets_into_env(
    *,
    chain_config_path: Path | None = None,
    storage_config_path: Path | None = None,
) -> list[str]:
    """Copy every stored provider secret into ``os.environ``.

    Returns the list of env-var names that were populated. Existing values
    in ``os.environ`` are NOT overwritten (dev workflows that want env-var
    overrides keep working).
    """
    backend = active_backend(storage_config_path or Path("config/storage_backend.yml"))
    if backend is None:
        return []

    providers = configured_providers(
        chain_config_path or Path("config/llm_chains.yml"),
    )
    populated: list[str] = []
    for pid in providers:
        entry = get_provider(pid)
        if entry is None:
            continue
        # Standard API keys.
        if entry.requires_key and entry.key_env_var:
            if os.environ.get(entry.key_env_var):
                continue  # Don't overwrite an explicit dev-time env var.
            value = backend.read_secret(entry.key_env_var)
            if value:
                os.environ[entry.key_env_var] = value
                populated.append(entry.key_env_var)
        # Ollama base URL is also stored in the backend (no key).
        if pid == "ollama" and not os.environ.get("OLLAMA_BASE_URL"):
            url = backend.read_secret("OLLAMA_BASE_URL")
            if url:
                os.environ["OLLAMA_BASE_URL"] = url
                populated.append("OLLAMA_BASE_URL")

    if populated:
        logger.info(
            "Loaded %d secret(s) from storage backend into environment: %s",
            len(populated), ", ".join(populated),
        )
    return populated
