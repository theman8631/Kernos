"""``kernos setup llm status`` — on-demand per-provider diagnostic.

Runs a full validation ping against each configured provider and prints a
readable readout. Separate from the startup health check, which is a
binary-only config read.

Zero LLM calls.
"""
from __future__ import annotations

import logging
from pathlib import Path

from kernos.setup.chain_config_io import configured_providers, load_chain_config
from kernos.setup.provider_registry import get_provider
from kernos.setup.storage_backend import active_backend, active_backend_name
from kernos.setup.validate import validate_key

logger = logging.getLogger(__name__)


def run_status(argv: list[str] | None = None) -> int:
    """Run `kernos setup llm status`. Returns POSIX exit code."""
    _argv = argv or []  # no flags today — kept for future

    config_path = Path("config/llm_chains.yml")
    storage_config_path = Path("config/storage_backend.yml")

    print("")
    print("Kernos LLM Status")
    print("=" * 40)
    print("")

    backend_name = active_backend_name(storage_config_path)
    print(f"Storage backend: {backend_name or '(not set)'}")
    print("")

    providers = sorted(configured_providers(config_path))
    if not providers:
        print("No providers configured.")
        print("Run `kernos setup llm` to add one.")
        return 1

    backend = active_backend(storage_config_path)
    cfg = load_chain_config(config_path)

    any_failure = False
    for pid in providers:
        entry = get_provider(pid)
        if entry is None:
            print(f"[{pid}]  unknown provider (not in registry)")
            any_failure = True
            continue

        print(f"[{entry.display_name}]")
        # Fetch key (or URL for Ollama).
        if entry.requires_key:
            if backend is None:
                print("  ✗ no storage backend set; can't read key")
                any_failure = True
                continue
            key = backend.read_secret(entry.key_env_var) or ""
            if not key:
                print(f"  ✗ no key stored under {entry.key_env_var}")
                any_failure = True
                continue
            result = validate_key(entry, api_key=key)
        else:
            # Ollama: URL may be overridden via OLLAMA_BASE_URL secret.
            url_base = ""
            if backend is not None:
                url_base = backend.read_secret("OLLAMA_BASE_URL") or ""
            override = (url_base.rstrip("/") + "/api/tags") if url_base else ""
            result = validate_key(entry, api_key="", override_url=override)

        if result.ok:
            print(f"  ✓ validated ({len(result.models)} models available)")
        else:
            print(f"  ✗ {result.error_kind}: {result.error_detail}")
            any_failure = True

        # List configured models from chain config.
        for chain_name, entries in cfg.items():
            for ent in entries:
                if ent.provider == pid:
                    print(f"      chain[{chain_name}] = {ent.model}")

    print("")
    return 1 if any_failure else 0
