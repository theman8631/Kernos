"""Handlers for the ``set_chain_model`` and ``diagnose_llm_chain`` admin tools.

Both tools are admin-only, system-space-only, Gate-authorized. Neither tool
performs an LLM call — they operate on the on-disk chain config, the
storage backend, and (for ``set_chain_model``) the provider's own
``/models`` endpoint for model-id validation.
"""
from __future__ import annotations

import logging
from pathlib import Path

from kernos.setup.chain_config_io import (
    configured_providers,
    load_chain_config,
    save_chain_config,
    set_chain_model_in_config,
)
from kernos.setup.provider_registry import get_provider
from kernos.setup.storage_backend import active_backend, active_backend_name
from kernos.setup.validate import validate_key

logger = logging.getLogger(__name__)

_CHAIN_NAMES = ("primary", "simple", "cheap")


def set_chain_model(
    *,
    chain: str,
    provider_id: str,
    model_id: str,
    chain_config_path: Path | None = None,
    storage_config_path: Path | None = None,
) -> dict:
    """Admin tool: swap the model for a (chain, provider) pair.

    Returns a dict with an ``ok`` flag and either ``message`` (success) or
    ``error`` (failure). Never raises.
    """
    if chain not in _CHAIN_NAMES:
        return {"ok": False, "error": f"Unknown chain: {chain!r}. Must be one of {_CHAIN_NAMES}."}

    provider = get_provider(provider_id)
    if provider is None:
        return {"ok": False, "error": f"Unknown provider: {provider_id!r}."}

    cfg = load_chain_config(chain_config_path)
    providers_configured = configured_providers(chain_config_path)
    if provider_id not in providers_configured:
        return {
            "ok": False,
            "error": (
                f"Provider {provider_id!r} is not currently configured. "
                "Run `kernos setup llm` to add it first."
            ),
        }

    # Validate the model id against the provider's /models endpoint.
    backend = active_backend(storage_config_path)
    key = ""
    override_url = ""
    if provider.requires_key:
        if backend is None:
            return {"ok": False, "error": "No storage backend configured."}
        key = backend.read_secret(provider.key_env_var) or ""
        if not key:
            return {
                "ok": False,
                "error": f"No stored credential for {provider.display_name}.",
            }
    else:
        # Ollama URL override.
        url_base = ""
        if backend is not None:
            url_base = backend.read_secret("OLLAMA_BASE_URL") or ""
        if url_base:
            override_url = url_base.rstrip("/") + "/api/tags"

    result = validate_key(provider, api_key=key, override_url=override_url)
    if not result.ok:
        return {
            "ok": False,
            "error": (
                f"Could not reach {provider.display_name} to validate model "
                f"list: {result.error_kind} ({result.error_detail})"
            ),
        }
    if model_id not in result.models:
        return {
            "ok": False,
            "error": (
                f"Model {model_id!r} is not in {provider.display_name}'s "
                f"current /models response. Available: "
                f"{', '.join(sorted(result.models)[:10])}"
                f"{'...' if len(result.models) > 10 else ''}"
            ),
        }

    new_cfg = set_chain_model_in_config(
        cfg, chain=chain, provider_id=provider_id, model=model_id,
    )
    save_chain_config(new_cfg, path=chain_config_path)
    logger.info(
        "CHAIN_MODEL_CHANGED: chain=%s provider=%s model=%s",
        chain, provider_id, model_id,
    )
    return {
        "ok": True,
        "message": (
            f"Model for chain {chain!r} + provider {provider_id!r} is now {model_id!r}."
        ),
        "event": "CHAIN_MODEL_CHANGED",
    }


def diagnose_llm_chain(
    *,
    include_fallback_events: bool = False,
    chain_config_path: Path | None = None,
    storage_config_path: Path | None = None,
    event_stream=None,
    instance_id: str = "",
) -> dict:
    """Admin tool: return a readable view of the current LLM chain config."""
    cfg = load_chain_config(chain_config_path)
    backend_name = active_backend_name(storage_config_path)
    backend = active_backend(storage_config_path)

    out: dict = {
        "ok": True,
        "storage_backend": backend_name,
        "chains": {},
    }
    for chain_name in _CHAIN_NAMES:
        entries = cfg.get(chain_name, [])
        rendered = []
        for entry in entries:
            provider = get_provider(entry.provider)
            has_key = False
            if provider is not None and provider.requires_key and backend is not None:
                has_key = backend.has_secret(provider.key_env_var)
            elif provider is not None and not provider.requires_key:
                has_key = True  # Ollama — no key needed.
            rendered.append({
                "provider": entry.provider,
                "model": entry.model,
                "has_credential": has_key,
            })
        out["chains"][chain_name] = rendered

    if include_fallback_events and event_stream is not None and instance_id:
        try:
            # FALLBACK_USED events are traced via the TurnEventCollector rather
            # than the main event stream by default. If/when they land in the
            # EventStream, this method can enumerate them; for now we return an
            # empty list and a note.
            out["fallback_events"] = []
            out["fallback_events_note"] = (
                "FALLBACK_USED events are traced per-turn via the runtime "
                "trace collector and not persisted in the main event stream."
            )
        except Exception as exc:
            out["fallback_events"] = []
            out["fallback_events_error"] = str(exc)

    return out
