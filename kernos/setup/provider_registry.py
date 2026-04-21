"""Built-in provider registry for `kernos setup llm`.

Seven providers, each with validation + model-discovery HTTP endpoints. This
is built-in data, not user-writable. Editing requires a code change.

`is_local_only` means the provider runs on the user's machine (no cloud
call, no API key). `requires_key` means setup must prompt for an API key.

`chain_memberships` lists which named chains the provider is eligible for —
purely informational for the setup flow's default suggestions; the agent's
chain-selection logic is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderEntry:
    """A single provider in the registry."""

    provider_id: str
    display_name: str
    is_local_only: bool
    requires_key: bool
    validation_endpoint: str
    models_endpoint: str
    chain_memberships: tuple[str, ...]
    auth_header: str
    auth_scheme: str = "Bearer"
    # Note (Ollama only): validation_endpoint is the models endpoint too; setup
    # overrides the default URL with the user-supplied Ollama URL.
    key_env_var: str = ""


REGISTRY: dict[str, ProviderEntry] = {
    "anthropic": ProviderEntry(
        provider_id="anthropic",
        display_name="Anthropic",
        is_local_only=False,
        requires_key=True,
        validation_endpoint="https://api.anthropic.com/v1/models",
        models_endpoint="https://api.anthropic.com/v1/models",
        chain_memberships=("primary", "simple", "cheap"),
        auth_header="x-api-key",
        auth_scheme="",  # raw key, not "Bearer <key>"
        key_env_var="ANTHROPIC_API_KEY",
    ),
    "openai": ProviderEntry(
        provider_id="openai",
        display_name="OpenAI",
        is_local_only=False,
        requires_key=True,
        validation_endpoint="https://api.openai.com/v1/models",
        models_endpoint="https://api.openai.com/v1/models",
        chain_memberships=("primary", "simple", "cheap"),
        auth_header="Authorization",
        auth_scheme="Bearer",
        key_env_var="OPENAI_API_KEY",
    ),
    "google": ProviderEntry(
        provider_id="google",
        display_name="Google (Gemini)",
        is_local_only=False,
        requires_key=True,
        # Gemini uses ?key=<key> query param, not a header. Our validation
        # code accepts an empty auth_header for query-param auth.
        validation_endpoint="https://generativelanguage.googleapis.com/v1beta/models",
        models_endpoint="https://generativelanguage.googleapis.com/v1beta/models",
        chain_memberships=("primary", "simple", "cheap"),
        auth_header="",
        auth_scheme="query:key",
        key_env_var="GOOGLE_API_KEY",
    ),
    "groq": ProviderEntry(
        provider_id="groq",
        display_name="Groq",
        is_local_only=False,
        requires_key=True,
        validation_endpoint="https://api.groq.com/openai/v1/models",
        models_endpoint="https://api.groq.com/openai/v1/models",
        chain_memberships=("simple", "cheap"),
        auth_header="Authorization",
        auth_scheme="Bearer",
        key_env_var="GROQ_API_KEY",
    ),
    "xai": ProviderEntry(
        provider_id="xai",
        display_name="xAI",
        is_local_only=False,
        requires_key=True,
        validation_endpoint="https://api.x.ai/v1/models",
        models_endpoint="https://api.x.ai/v1/models",
        chain_memberships=("primary", "simple", "cheap"),
        auth_header="Authorization",
        auth_scheme="Bearer",
        key_env_var="XAI_API_KEY",
    ),
    "openrouter": ProviderEntry(
        provider_id="openrouter",
        display_name="OpenRouter",
        is_local_only=False,
        requires_key=True,
        validation_endpoint="https://openrouter.ai/api/v1/models",
        models_endpoint="https://openrouter.ai/api/v1/models",
        chain_memberships=("primary", "simple", "cheap"),
        auth_header="Authorization",
        auth_scheme="Bearer",
        key_env_var="OPENROUTER_API_KEY",
    ),
    "ollama": ProviderEntry(
        provider_id="ollama",
        display_name="Ollama (local)",
        is_local_only=True,
        requires_key=False,
        # Default URL; setup accepts override.
        validation_endpoint="http://localhost:11434/api/tags",
        models_endpoint="http://localhost:11434/api/tags",
        chain_memberships=("simple", "cheap"),
        auth_header="",
        auth_scheme="",
        key_env_var="",
    ),
}


def list_providers() -> list[ProviderEntry]:
    """Return providers in the canonical display order."""
    order = ["anthropic", "openai", "google", "groq", "xai", "openrouter", "ollama"]
    return [REGISTRY[pid] for pid in order]


def get_provider(provider_id: str) -> ProviderEntry | None:
    """Look up a provider by id, or return None."""
    return REGISTRY.get(provider_id)
