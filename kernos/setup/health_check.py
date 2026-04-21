"""Startup binary health check.

For each named chain declared in the on-disk chain config, is there at
least one provider with a stored credential?

**Binary** in the precise sense: file IO only, no network, no LLM calls,
no credential-freshness validation. Pure config-read.

This is called from ``kernos start`` (and the other top-level entry points)
before any provider or LLM client is instantiated. Exit code 1 on any
failure. The agent never starts from a "no chain works" state — the user
has to run ``kernos setup llm`` first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from kernos.setup.chain_config_io import load_chain_config
from kernos.setup.provider_registry import get_provider
from kernos.setup.storage_backend import active_backend

logger = logging.getLogger(__name__)

_EXPECTED_CHAINS = ("primary", "simple", "cheap")


@dataclass(frozen=True)
class HealthCheckResult:
    ok: bool
    failing_chains: tuple[str, ...]
    reason: str


def check_llm_chain_health(
    *,
    chain_config_path: Path | None = None,
    storage_config_path: Path | None = None,
) -> HealthCheckResult:
    """Binary config-read. No network, no LLM, no credential validation."""
    cfg = load_chain_config(chain_config_path or Path("config/llm_chains.yml"))
    backend = active_backend(storage_config_path or Path("config/storage_backend.yml"))

    if not cfg:
        return HealthCheckResult(
            ok=False,
            failing_chains=_EXPECTED_CHAINS,
            reason=(
                "No LLM chain configured. "
                "Run `kernos setup llm` to configure providers."
            ),
        )

    failing: list[str] = []
    for chain_name in _EXPECTED_CHAINS:
        entries = cfg.get(chain_name, [])
        if not entries:
            failing.append(chain_name)
            continue
        # At least one entry with a stored credential passes this chain.
        chain_ok = False
        for entry in entries:
            provider = get_provider(entry.provider)
            if provider is None:
                continue
            if not provider.requires_key:
                # Ollama — presence in the chain is sufficient.
                chain_ok = True
                break
            if backend is None:
                continue
            if backend.has_secret(provider.key_env_var):
                chain_ok = True
                break
        if not chain_ok:
            failing.append(chain_name)

    if failing:
        msg = (
            f"Chain '{failing[0]}' has no providers configured. "
            f"Run `kernos setup llm` to configure it."
        )
        if len(failing) > 1:
            msg = (
                f"Chains {failing} have no providers configured. "
                f"Run `kernos setup llm`."
            )
        return HealthCheckResult(
            ok=False, failing_chains=tuple(failing), reason=msg,
        )

    return HealthCheckResult(ok=True, failing_chains=(), reason="")


def enforce_or_exit() -> None:
    """Binary health check + secret loading. Call from entry points.

    1. Binary config check — must pass, else exit 1.
    2. Load secrets from the active storage backend into ``os.environ`` so the
       existing ``build_chains_from_env()`` path finds them.

    Step 2 is not part of the "health check" itself (which is binary, no IO
    beyond the config files) — it's the adjacent startup step that reads the
    stored credentials. Kept in one entrypoint for convenience.
    """
    result = check_llm_chain_health()
    if not result.ok:
        import sys

        print("Kernos startup: LLM chain configuration check failed.")
        print(f"  {result.reason}")
        sys.exit(1)
    # Binary check passed — now pull secrets into the environment.
    from kernos.setup.env_loader import load_secrets_into_env

    load_secrets_into_env()
