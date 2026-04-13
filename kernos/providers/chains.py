"""Data-driven provider chain configuration.

Builds ChainConfig from environment variables today. Tomorrow, a
build_chains_from_json(path) function returns the same ChainConfig type —
zero consumer changes needed.
"""
from __future__ import annotations

import logging
import os

from kernos.providers.base import ChainConfig, ChainEntry, Provider

logger = logging.getLogger(__name__)


def _instantiate_provider(spec: str) -> Provider:
    """Instantiate a single provider from a spec string.

    Supported specs:
        "anthropic"           → AnthropicProvider
        "openai-codex"        → OpenAICodexProvider
        "ollama"              → OllamaProvider (default model from env)
        "ollama:model_tag"    → OllamaProvider(model=model_tag)
    """
    if spec.startswith("ollama:"):
        from kernos.providers.ollama_provider import OllamaProvider
        model = spec[len("ollama:"):]
        return OllamaProvider(model=model)
    if spec == "ollama":
        from kernos.providers.ollama_provider import OllamaProvider
        return OllamaProvider()
    if spec == "openai-codex":
        from kernos.kernel.credentials import resolve_openai_codex_credential
        from kernos.providers.codex_provider import OpenAICodexProvider
        return OpenAICodexProvider(credential=resolve_openai_codex_credential())
    if spec == "anthropic":
        from kernos.kernel.credentials import resolve_anthropic_credential
        from kernos.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=resolve_anthropic_credential())
    raise ValueError(f"Unknown provider spec: {spec!r}")


def _build_chain_entries(providers: list[Provider], model_attr: str, fallback_model: str) -> list[ChainEntry]:
    """Build a chain entry list using a specific model attribute from each provider."""
    entries: list[ChainEntry] = []
    for p in providers:
        model = getattr(p, model_attr, fallback_model)
        entries.append(ChainEntry(provider=p, model=model))
    return entries


def build_chains_from_env() -> tuple[ChainConfig, Provider]:
    """Build provider chains from KERNOS_LLM_PROVIDER and KERNOS_LLM_FALLBACK env vars.

    Returns (chains, primary_provider).
    """
    # Primary provider
    primary_spec = os.getenv("KERNOS_LLM_PROVIDER", "anthropic")
    primary = _instantiate_provider(primary_spec)
    logger.info("Primary provider: %s/%s", getattr(primary, "provider_name", "unknown"), getattr(primary, "main_model", "unknown"))

    # Fallback providers
    all_providers = [primary]
    fallback_spec = os.getenv("KERNOS_LLM_FALLBACK", "")
    if fallback_spec:
        for entry in fallback_spec.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                fb = _instantiate_provider(entry)
                all_providers.append(fb)
                logger.info("Fallback provider ready: %s/%s", getattr(fb, "provider_name", "unknown"), getattr(fb, "main_model", "unknown"))
            except Exception as exc:
                logger.warning("Failed to init fallback provider %s: %s", entry, exc)

    # Build three chains from the ordered provider list
    chains: ChainConfig = {
        "primary": _build_chain_entries(all_providers, "main_model", "unknown"),
        "simple": _build_chain_entries(all_providers, "simple_model", "unknown"),
        "cheap": _build_chain_entries(all_providers, "cheap_model", "unknown"),
    }

    for name, entries in chains.items():
        models = [f"{getattr(e.provider, 'provider_name', '?')}/{e.model}" for e in entries]
        logger.info("Chain[%s]: %s", name, " → ".join(models))

    return chains, primary
