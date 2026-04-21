"""Deterministic HTTP validation + model discovery for providers.

Used at setup time (``kernos setup llm``) and on-demand (``kernos setup llm
status``). ZERO LLM calls — just the provider's own ``/models`` endpoint (or
``/api/tags`` for Ollama).

Returns structured results so the caller can produce specific error messages
on network failures, auth failures, and rate-limit responses.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from kernos.setup.provider_registry import ProviderEntry

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15


@dataclass
class ValidationResult:
    ok: bool
    # Populated on success:
    models: list[str]
    # Populated on failure:
    error_kind: str = ""     # "network" | "auth" | "rate_limit" | "unexpected" | "parse"
    error_detail: str = ""


def _build_request(
    provider: ProviderEntry, *, api_key: str, override_url: str = "",
) -> urllib.request.Request:
    """Build an HTTP request for the provider's models endpoint."""
    url = override_url or provider.models_endpoint

    headers: dict[str, str] = {
        "User-Agent": "kernos-setup/1.0",
        "Accept": "application/json",
    }

    if provider.auth_scheme.startswith("query:"):
        # Gemini-style: ?key=<key>
        param_name = provider.auth_scheme.split(":", 1)[1]
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({param_name: api_key})}"
    elif provider.auth_header:
        if provider.auth_scheme:
            headers[provider.auth_header] = f"{provider.auth_scheme} {api_key}"
        else:
            headers[provider.auth_header] = api_key

    # Anthropic requires an anthropic-version header.
    if provider.provider_id == "anthropic":
        headers["anthropic-version"] = "2023-06-01"

    return urllib.request.Request(url, headers=headers, method="GET")


def _parse_models(provider_id: str, payload: dict | list) -> list[str]:
    """Extract model ids from a provider response. Best-effort across shapes."""
    # Ollama: {"models": [{"name": "..."}, ...]}
    if provider_id == "ollama":
        if isinstance(payload, dict) and isinstance(payload.get("models"), list):
            return [
                m.get("name", "") for m in payload["models"]
                if isinstance(m, dict) and m.get("name")
            ]
        return []

    # Google: {"models": [{"name": "models/gemini-...", ...}, ...]}
    if provider_id == "google":
        if isinstance(payload, dict) and isinstance(payload.get("models"), list):
            ids: list[str] = []
            for m in payload["models"]:
                if not isinstance(m, dict):
                    continue
                name = m.get("name", "")
                if name.startswith("models/"):
                    ids.append(name[len("models/"):])
                elif name:
                    ids.append(name)
            return ids
        return []

    # OpenAI / OpenRouter / Groq / xAI: {"data": [{"id": "..."}, ...]}
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [
            m.get("id", "") for m in payload["data"]
            if isinstance(m, dict) and m.get("id")
        ]

    # Anthropic: {"data": [{"id": "..."}, ...]}
    if isinstance(payload, list):
        return [
            m.get("id", "") for m in payload
            if isinstance(m, dict) and m.get("id")
        ]
    return []


def validate_key(
    provider: ProviderEntry, *, api_key: str, override_url: str = "",
) -> ValidationResult:
    """Validate an API key by fetching the provider's ``/models`` endpoint.

    Returns a ``ValidationResult`` with ``ok=True`` and the model id list on
    success, or ``ok=False`` + an ``error_kind`` on any failure. Never raises.
    """
    # Ollama has no key — validate the URL instead.
    if provider.is_local_only and not provider.requires_key:
        return _probe_local(provider, override_url=override_url)

    req = _build_request(provider, api_key=api_key, override_url=override_url)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or exc.code == 403:
            return ValidationResult(ok=False, models=[], error_kind="auth",
                                    error_detail=f"HTTP {exc.code} from {provider.display_name}")
        if exc.code == 429:
            return ValidationResult(ok=False, models=[], error_kind="rate_limit",
                                    error_detail=f"HTTP 429 from {provider.display_name}")
        return ValidationResult(ok=False, models=[], error_kind="unexpected",
                                error_detail=f"HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return ValidationResult(ok=False, models=[], error_kind="network",
                                error_detail=str(exc.reason))
    except TimeoutError:
        return ValidationResult(ok=False, models=[], error_kind="network",
                                error_detail="timeout")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return ValidationResult(ok=False, models=[], error_kind="parse",
                                error_detail=str(exc))

    return ValidationResult(ok=True, models=_parse_models(provider.provider_id, payload))


def _probe_local(provider: ProviderEntry, *, override_url: str = "") -> ValidationResult:
    """Probe Ollama (or another local server) via its URL. No key involved."""
    url = override_url or provider.models_endpoint
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "kernos-setup/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return ValidationResult(ok=False, models=[], error_kind="network",
                                error_detail=f"Cannot reach {url}: {reason}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return ValidationResult(ok=False, models=[], error_kind="parse",
                                error_detail=str(exc))
    return ValidationResult(ok=True, models=_parse_models(provider.provider_id, payload))
