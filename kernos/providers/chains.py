"""Data-driven provider chain configuration.

Builds ChainConfig from environment variables today. Tomorrow, a
build_chains_from_json(path) function returns the same ChainConfig type —
zero consumer changes needed.

Runtime source of truth. Kernos startup (``kernos/setup/health_check.py``)
asks :func:`can_build_chains_from_env` — a side-effect-free dry-run of
``build_chains_from_env`` — for its "will this install actually run?"
answer. The setup-time YAML at ``config/llm_chains.yml`` is wizard
bookkeeping, not a startup gate (see MODEL-SELECTION-MODULE for the
eventual consolidation).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# Startup dry-run — "will this install actually run?"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanBuildResult:
    """Outcome of the side-effect-free dry-run.

    ``ok`` is True when the primary provider (and at least its class
    import + credential resolution) would succeed. Fallback providers
    are reported per-spec in ``unresolved`` when they'd skip — that
    mirrors the runtime's tolerance (fallbacks are optional).
    """
    ok: bool
    primary_spec: str = ""
    resolved: tuple[str, ...] = ()
    unresolved: tuple[tuple[str, str], ...] = ()  # (spec, reason) pairs
    reason: str = ""


def _can_resolve_provider(spec: str) -> tuple[bool, str]:
    """Dry-run one provider spec without constructing a Provider.

    Side-effect profile:
      - imports the provider class module (fast, deterministic)
      - calls the pure credential resolver (env reads + file reads only)
      - no httpx.AsyncClient, no anthropic.AsyncAnthropic, no network,
        no auth ping
    """
    try:
        if spec.startswith("ollama:") or spec == "ollama":
            # Ollama has no credential to resolve — class import is the check.
            from kernos.providers import ollama_provider  # noqa: F401
            return True, ""
        if spec == "openai-codex":
            from kernos.providers import codex_provider  # noqa: F401
            from kernos.kernel.credentials import resolve_openai_codex_credential
            try:
                resolve_openai_codex_credential()
            except Exception as exc:
                return False, f"openai-codex credential missing: {exc}"
            return True, ""
        if spec == "anthropic":
            from kernos.providers import anthropic_provider  # noqa: F401
            from kernos.kernel.credentials import resolve_anthropic_credential
            cred = resolve_anthropic_credential()
            if not cred:
                return False, "anthropic credential missing (ANTHROPIC_API_KEY or OAuth)"
            return True, ""
        return False, f"unknown provider spec: {spec!r}"
    except ImportError as exc:
        return False, f"provider module not importable for {spec!r}: {exc}"
    except Exception as exc:  # noqa: BLE001 — dry-run must not raise
        return False, f"unexpected dry-run error for {spec!r}: {exc}"


def can_build_chains_from_env() -> CanBuildResult:
    """Side-effect-free dry-run of :func:`build_chains_from_env`.

    Validates that the env-configured chain is structurally resolvable:
    the primary provider class imports and its credentials resolve.
    Fallback specs are probed the same way but don't block the check —
    the runtime tolerates fallback failures by skipping.

    Never instantiates a ``Provider`` subclass. Never opens a network
    connection or touches an LLM API. Safe to call at startup before any
    provider client has spun up.
    """
    primary_spec = os.getenv("KERNOS_LLM_PROVIDER", "anthropic").strip()
    if not primary_spec:
        return CanBuildResult(
            ok=False, reason="KERNOS_LLM_PROVIDER is empty (no primary provider configured)",
        )

    resolved: list[str] = []
    unresolved: list[tuple[str, str]] = []

    ok, reason = _can_resolve_provider(primary_spec)
    if not ok:
        return CanBuildResult(
            ok=False,
            primary_spec=primary_spec,
            unresolved=((primary_spec, reason),),
            reason=(
                f"Primary provider '{primary_spec}' not usable: {reason}. "
                f"Configure credentials in .env or run `kernos setup llm`."
            ),
        )
    resolved.append(primary_spec)

    fallback_spec = os.getenv("KERNOS_LLM_FALLBACK", "")
    for entry in (s.strip() for s in fallback_spec.split(",") if s.strip()):
        ok, reason = _can_resolve_provider(entry)
        if ok:
            resolved.append(entry)
        else:
            unresolved.append((entry, reason))

    return CanBuildResult(
        ok=True,
        primary_spec=primary_spec,
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
    )
