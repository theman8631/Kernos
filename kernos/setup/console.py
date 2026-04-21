"""``kernos setup llm`` — interactive LLM setup console.

Linear, deterministic, terminal-driven. Zero LLM calls.

Three entry modes:
  * **fresh install** — first-run detection prompts the user through
    provider selection before the server can even start.
  * **user-initiated adjust/add** — re-run ``kernos setup llm`` anytime
    to add a provider, change storage backend, or remove a provider.
  * **recovery from runtime failure** — invoked after a chain-exhaustion
    event; same UX as adjust.

Two-chain setup UX: asks about ``primary`` (S-class, best intelligence)
and ``cheap`` (B-class, fast/efficient). ``simple`` inherits from
``cheap`` at configuration time; users can override post-setup via
``set_chain_model``.

Setup configures the LLM chain, so setup cannot depend on the LLM chain.
This module imports NO LLM client and makes NO model inference calls —
only provider-owned ``/models`` HTTP endpoints for validation, the
benchmark snapshot for recommendations, and deterministic console I/O.
"""
from __future__ import annotations

import getpass
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from kernos.setup.benchmark_snapshot import (
    BenchmarkSnapshotError,
    recommended_models,
)
from kernos.setup.chain_config_io import (
    add_provider_to_chains,
    configured_providers,
    load_chain_config,
    remove_provider_from_chains,
    save_chain_config,
)
from kernos.setup.provider_registry import (
    ProviderEntry,
    get_provider,
    list_providers,
)
from kernos.setup.storage_backend import (
    StorageBackendSwitchAborted,
    StorageBackendUnavailable,
    VALID_BACKENDS,
    active_backend,
    active_backend_name,
    detect_default_backend,
    get_backend,
    switch_storage_backend,
)
from kernos.setup.validate import validate_key

logger = logging.getLogger(__name__)

_SEPARATOR = "=" * 40


# ---------------------------------------------------------------------------
# Small IO helpers — isolated so tests can swap them out.
# ---------------------------------------------------------------------------


def _echo(msg: str = "") -> None:
    print(msg, flush=True)


def _prompt(msg: str) -> str:
    return input(f"{msg}").strip()


def _prompt_secret(msg: str) -> str:
    return getpass.getpass(msg).strip()


def _prompt_yes_no(msg: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = _prompt(f"{msg} {suffix}: ").lower()
    if not raw:
        return default
    return raw.startswith("y")


# ---------------------------------------------------------------------------
# Console state
# ---------------------------------------------------------------------------


@dataclass
class SetupState:
    """Aggregate state for one setup session."""

    config_path: Path
    storage_config_path: Path
    managed_env_vars: list[str]  # All known key_env_var values from the registry

    @classmethod
    def default(cls) -> "SetupState":
        return cls(
            config_path=Path("config/llm_chains.yml"),
            storage_config_path=Path("config/storage_backend.yml"),
            managed_env_vars=[
                p.key_env_var for p in list_providers() if p.key_env_var
            ],
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_setup(argv: list[str] | None = None) -> int:
    """Run ``kernos setup llm`` end-to-end. Returns a POSIX exit code."""
    args = argv or []
    if args and args[0] == "status":
        from kernos.setup.status import run_status

        return run_status(args[1:])

    state = SetupState.default()
    _echo("")
    _echo("Kernos LLM Setup")
    _echo(_SEPARATOR)
    _echo("")

    _show_current_state(state)

    while True:
        action = _choose_action(state)
        if action == "quit":
            break
        if action == "add":
            _add_provider_flow(state)
        elif action == "remove":
            _remove_provider_flow(state)
        elif action == "switch_storage":
            _switch_storage_flow(state)
        else:
            _echo(f"Unknown action: {action!r}")
        _echo("")

    _echo("Setup complete.")
    return 0


# ---------------------------------------------------------------------------
# UI pieces
# ---------------------------------------------------------------------------


def _show_current_state(state: SetupState) -> None:
    cfg = load_chain_config(state.config_path)
    providers = sorted(configured_providers(state.config_path))
    if providers:
        _echo(f"Current providers: {', '.join(providers)}")
    else:
        _echo("Current providers: (none)")
    backend = active_backend_name(state.storage_config_path)
    if backend:
        _echo(f"Storage backend:   {backend}")
    _echo("")


def _choose_action(state: SetupState) -> str:
    configured = configured_providers(state.config_path)
    _echo("What would you like to do?")
    _echo("  1) Add a provider")
    _echo("  2) Remove a provider")
    _echo("  3) Change storage backend")
    _echo("  q) Quit")
    _echo("")
    while True:
        raw = _prompt("> ").strip().lower()
        if raw in {"1", "add"}:
            return "add"
        if raw in {"2", "remove"}:
            if not configured:
                _echo("No providers configured yet — add one first.")
                continue
            return "remove"
        if raw in {"3", "storage", "switch", "backend"}:
            return "switch_storage"
        if raw in {"q", "quit", "exit"}:
            return "quit"
        _echo("Please enter 1, 2, 3, or q.")


def _add_provider_flow(state: SetupState) -> None:
    provider = _pick_provider()
    if provider is None:
        return

    # Ollama: no key, URL-based validation.
    if provider.is_local_only and not provider.requires_key:
        _add_local_provider_flow(state, provider)
        return

    api_key = _prompt_secret(f"Paste your {provider.display_name} API key (hidden): ")
    if not api_key:
        _echo("No key entered — cancelled.")
        return

    _echo(f"Validating key with {provider.display_name}...", )
    result = validate_key(provider, api_key=api_key)
    if not result.ok:
        _print_validation_error(provider, result)
        return
    _echo(f"OK. Fetching available models... found {len(result.models)} models.")
    _echo("")

    primary, cheap = _pick_models_for_chains(provider, result.models)
    if not primary and not cheap:
        _echo("No model selected — cancelled.")
        return

    # Storage backend: prompt if this is the first provider, else reuse.
    backend_name = active_backend_name(state.storage_config_path)
    if backend_name is None:
        backend_name = _pick_storage_backend(state)
        if backend_name is None:
            _echo("Cancelled.")
            return
        # Record the backend choice (first-time set — no secrets to migrate yet).
        try:
            switch_storage_backend(
                backend_name,
                state.managed_env_vars,
                config_path=state.storage_config_path,
            )
        except (StorageBackendUnavailable, StorageBackendSwitchAborted) as exc:
            _echo(f"Could not set storage backend: {exc}")
            return

    # Write the key to the active backend.
    backend = get_backend(backend_name)
    backend.write_secret(provider.key_env_var, api_key)

    # Update chain config.
    cfg = load_chain_config(state.config_path)
    new_cfg = add_provider_to_chains(
        cfg,
        provider_id=provider.provider_id,
        primary_model=primary,
        cheap_model=cheap,
    )
    save_chain_config(new_cfg, path=state.config_path)

    _echo("")
    _echo(f"{provider.display_name} configured ({backend_name} storage).")
    if primary:
        _echo(f"  main intelligence: {primary}")
    if cheap:
        _echo(f"  fast / efficient:  {cheap}")
    _echo("")


def _add_local_provider_flow(state: SetupState, provider: ProviderEntry) -> None:
    """Ollama path: URL-based validation, no key, no storage backend needed."""
    _echo(
        f"{provider.display_name} is a local-models option — models run on "
        f"your machine, no cloud calls."
    )
    default_url = provider.validation_endpoint
    url = _prompt(f"Ollama URL? [default: {default_url.split('/api')[0]}] ")
    if not url:
        url = default_url
    else:
        # User supplied a base URL; append /api/tags.
        if "/api/tags" not in url:
            url = url.rstrip("/") + "/api/tags"

    _echo(f"Checking {url}...")
    result = validate_key(provider, api_key="", override_url=url)
    if not result.ok:
        _print_validation_error(provider, result)
        return
    _echo(f"OK. Found {len(result.models)} local models.")

    primary, cheap = _pick_models_for_chains(provider, result.models)
    if not primary and not cheap:
        _echo("No model selected — cancelled.")
        return

    cfg = load_chain_config(state.config_path)
    new_cfg = add_provider_to_chains(
        cfg,
        provider_id=provider.provider_id,
        primary_model=primary,
        cheap_model=cheap,
    )
    save_chain_config(new_cfg, path=state.config_path)

    # Persist the Ollama URL in an env-var style key so runtime can rebuild
    # the client against the chosen URL. We use env_hardened as a default if
    # no backend has been chosen yet, since Ollama has no secret.
    backend_name = active_backend_name(state.storage_config_path)
    if backend_name is None:
        backend_name = detect_default_backend()
        try:
            switch_storage_backend(
                backend_name,
                state.managed_env_vars,
                config_path=state.storage_config_path,
            )
        except (StorageBackendUnavailable, StorageBackendSwitchAborted) as exc:
            _echo(f"Could not set storage backend: {exc}")
            return

    backend = get_backend(backend_name)
    backend.write_secret("OLLAMA_BASE_URL", url.replace("/api/tags", ""))

    _echo("")
    _echo(f"{provider.display_name} configured at {url.replace('/api/tags', '')}.")
    if primary:
        _echo(f"  main intelligence: {primary}")
    if cheap:
        _echo(f"  fast / efficient:  {cheap}")
    _echo("")


def _pick_provider() -> ProviderEntry | None:
    _echo("Which provider would you like to add?")
    providers = list_providers()
    for i, p in enumerate(providers, start=1):
        tag = " (local)" if p.is_local_only else ""
        _echo(f"  {i}) {p.display_name}{tag}")
    _echo("  q) Cancel")
    _echo("")
    while True:
        raw = _prompt("> ").strip().lower()
        if raw in {"q", "quit", "cancel"}:
            return None
        try:
            idx = int(raw)
        except ValueError:
            _echo(f"Please enter a number 1-{len(providers)} or q.")
            continue
        if not 1 <= idx <= len(providers):
            _echo(f"Please enter a number 1-{len(providers)} or q.")
            continue
        return providers[idx - 1]


def _pick_models_for_chains(
    provider: ProviderEntry, available_models: list[str],
) -> tuple[str, str]:
    """Pick primary + cheap models. Returns (primary, cheap). Either may be empty."""
    try:
        recs = recommended_models(provider.provider_id)
    except BenchmarkSnapshotError as exc:
        logger.warning("Benchmark snapshot unreadable: %s", exc)
        recs = {}

    primary_rec = recs.get("primary", "")
    cheap_rec = recs.get("cheap", "")

    # If the rec is in the available list, offer accept/override/skip.
    # If not, show the full list.
    primary = _pick_single_model(
        "main intelligence", primary_rec, available_models,
    )
    cheap = _pick_single_model(
        "fast / efficient", cheap_rec, available_models,
    )
    return primary, cheap


def _pick_single_model(
    label: str, recommendation: str, available: list[str],
) -> str:
    """Prompt for a model, defaulting to ``recommendation`` if it's available."""
    rec_available = recommendation and recommendation in available
    if rec_available:
        _echo(f"Kernos recommends for {label}: {recommendation}")
        choice = _prompt(f"Accept {recommendation}? [Y/n/list] ").strip().lower()
        if choice in {"", "y", "yes"}:
            return recommendation
        if choice not in {"l", "list", "n", "no"}:
            # Treat direct model-id entry as override.
            if choice in available:
                return choice
        # Fall through to list picker.
    else:
        if recommendation:
            _echo(
                f"(Kernos would recommend {recommendation} for {label}, but that "
                f"model isn't currently available from this provider.)"
            )

    # Show list.
    if not available:
        _echo(f"No models available for {label}.")
        return ""
    _echo(f"Available models for {label}:")
    for i, m in enumerate(available, start=1):
        _echo(f"  {i}) {m}")
    _echo("  s) Skip (no model for this chain tier)")
    while True:
        raw = _prompt("> ").strip().lower()
        if raw in {"s", "skip"}:
            return ""
        try:
            idx = int(raw)
        except ValueError:
            # Allow typing the model id directly.
            if raw in available:
                return raw
            _echo(f"Please enter a number 1-{len(available)}, the model id, or s.")
            continue
        if not 1 <= idx <= len(available):
            _echo(f"Please enter a number 1-{len(available)}, the model id, or s.")
            continue
        return available[idx - 1]


def _pick_storage_backend(state: SetupState) -> "str | None":
    _echo("Store keys how?")
    _echo("  1) OS keychain (recommended — keys never touch disk as files)")
    _echo("  2) Hardened .env file (0600 permissions)")
    _echo("  3) Plaintext .env (explicit opt-in required)")
    _echo("  q) Cancel")
    while True:
        raw = _prompt("> ").strip().lower()
        if raw in {"q", "quit", "cancel"}:
            return None
        if raw == "1":
            return "keychain"
        if raw == "2":
            return "env_hardened"
        if raw == "3":
            phrase = _prompt(
                'Plaintext storage requires explicit opt-in. Type "yes, I accept '
                'plaintext storage" to continue, anything else to cancel: '
            )
            if phrase.strip().lower() == "yes, i accept plaintext storage":
                return "env_plaintext"
            _echo("Opt-in phrase not matched. Cancelled.")
            return None
        _echo("Please enter 1, 2, 3, or q.")


def _switch_storage_flow(state: SetupState) -> None:
    current = active_backend_name(state.storage_config_path)
    _echo(f"Current backend: {current or '(none)'}")
    target = _pick_storage_backend(state)
    if target is None:
        _echo("Cancelled.")
        return
    if target == current:
        _echo("Same as current — no change.")
        return
    _echo(f"Switching to {target} (new write → verify → old remove)...")
    try:
        switch_storage_backend(
            target,
            state.managed_env_vars,
            config_path=state.storage_config_path,
        )
    except StorageBackendSwitchAborted as exc:
        _echo(f"Aborted: {exc}")
        _echo(f"Old backend ({current}) left untouched.")
        return
    except StorageBackendUnavailable as exc:
        _echo(f"Target backend unavailable: {exc}")
        return
    _echo("Switch complete.")


def _remove_provider_flow(state: SetupState) -> None:
    configured = sorted(configured_providers(state.config_path))
    if not configured:
        _echo("No providers configured.")
        return
    _echo("Which provider would you like to remove?")
    for i, pid in enumerate(configured, start=1):
        _echo(f"  {i}) {pid}")
    _echo("  q) Cancel")
    while True:
        raw = _prompt("> ").strip().lower()
        if raw in {"q", "quit", "cancel"}:
            return
        try:
            idx = int(raw)
        except ValueError:
            _echo(f"Please enter a number 1-{len(configured)} or q.")
            continue
        if not 1 <= idx <= len(configured):
            _echo(f"Please enter a number 1-{len(configured)} or q.")
            continue
        pid = configured[idx - 1]
        break

    provider = get_provider(pid)
    # Drop the credential from the active backend.
    backend = active_backend(state.storage_config_path)
    if backend is not None and provider is not None and provider.key_env_var:
        backend.remove_secret(provider.key_env_var)
    # Drop from the chain config.
    cfg = load_chain_config(state.config_path)
    new_cfg = remove_provider_from_chains(cfg, pid)
    save_chain_config(new_cfg, path=state.config_path)
    _echo(f"Removed provider: {pid}")


def _print_validation_error(provider: ProviderEntry, result) -> None:
    if result.error_kind == "auth":
        _echo(
            f"{provider.display_name} rejected the key (auth failure). "
            f"Detail: {result.error_detail}"
        )
    elif result.error_kind == "rate_limit":
        _echo(
            f"{provider.display_name} returned a rate-limit response. Try again "
            f"shortly. Detail: {result.error_detail}"
        )
    elif result.error_kind == "network":
        _echo(
            f"Could not reach {provider.display_name} (network / connectivity). "
            f"Detail: {result.error_detail}"
        )
    elif result.error_kind == "parse":
        _echo(
            f"{provider.display_name} responded but the response was unparseable. "
            f"Detail: {result.error_detail}"
        )
    else:
        _echo(
            f"Validation failed for {provider.display_name}: "
            f"{result.error_detail}"
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_setup(sys.argv[1:]))
