"""Read/write ``ChainConfig`` to a persistent on-disk source of truth.

Setup writes to ``config/llm_chains.yml``. ``build_chains_from_yaml(path)``
in ``kernos.providers.chains`` reads from it. Env vars still override for
dev flows.

Schema (YAML):

    chains:
      primary:
        - {provider: anthropic, model: claude-opus-4-7}
        - {provider: openai,    model: gpt-5.5}
      cheap:
        - {provider: anthropic, model: claude-haiku-4-5}
      simple:
        - {provider: anthropic, model: claude-haiku-4-5}

Order inside each list is the fallback order. The first entry is the
primary pick; subsequent entries are fallbacks for chain-exhaustion
purposes.

**Zero-LLM-call:** file IO only. No LLM imports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/llm_chains.yml")


@dataclass
class ChainEntrySpec:
    provider: str
    model: str


ChainConfigSpec = dict[str, list[ChainEntrySpec]]


def load_chain_config(path: Path | None = None) -> ChainConfigSpec:
    """Load the chain config from YAML; return empty dict if missing."""
    import yaml

    p = path or _CONFIG_PATH
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        logger.warning("llm_chains.yml malformed: %s", exc)
        return {}
    chains = raw.get("chains", {}) or {}
    out: ChainConfigSpec = {}
    for chain_name, entries in chains.items():
        if not isinstance(entries, list):
            continue
        parsed: list[ChainEntrySpec] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            provider = e.get("provider", "")
            model = e.get("model", "")
            if provider and model:
                parsed.append(ChainEntrySpec(provider=provider, model=model))
        if parsed:
            out[chain_name] = parsed
    return out


def save_chain_config(
    config: ChainConfigSpec, *, path: Path | None = None,
) -> None:
    """Atomically write the chain config to YAML."""
    import yaml

    p = path or _CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    serializable = {
        "chains": {
            chain: [{"provider": e.provider, "model": e.model} for e in entries]
            for chain, entries in config.items()
        }
    }
    p.write_text(yaml.safe_dump(serializable, sort_keys=False))


def configured_providers(path: Path | None = None) -> set[str]:
    """Return the set of ``provider_id``s referenced anywhere in the chain config."""
    cfg = load_chain_config(path)
    providers: set[str] = set()
    for entries in cfg.values():
        for entry in entries:
            providers.add(entry.provider)
    return providers


def add_provider_to_chains(
    config: ChainConfigSpec,
    *,
    provider_id: str,
    primary_model: str,
    cheap_model: str,
) -> ChainConfigSpec:
    """Append a provider's ``primary`` and ``cheap`` models to the chains.

    ``simple`` inherits from ``cheap`` unless explicitly overridden later via
    ``set_chain_model``. Here we mirror the cheap entry into simple so the
    simple chain has at least one provider.

    Returns a new config dict (does not mutate the argument).
    """
    new = {k: list(v) for k, v in config.items()}
    for chain_name, model in (("primary", primary_model), ("cheap", cheap_model), ("simple", cheap_model)):
        if not model:
            continue
        new.setdefault(chain_name, []).append(ChainEntrySpec(provider=provider_id, model=model))
    return new


def set_chain_model_in_config(
    config: ChainConfigSpec,
    *,
    chain: str,
    provider_id: str,
    model: str,
) -> ChainConfigSpec:
    """Replace / set the model for a (chain, provider) pair.

    If the (chain, provider) pair exists, update its model. Otherwise append
    a new entry at the end of the chain.
    """
    new = {k: list(v) for k, v in config.items()}
    entries = new.setdefault(chain, [])
    for i, entry in enumerate(entries):
        if entry.provider == provider_id:
            entries[i] = ChainEntrySpec(provider=provider_id, model=model)
            return new
    entries.append(ChainEntrySpec(provider=provider_id, model=model))
    return new


def remove_provider_from_chains(
    config: ChainConfigSpec, provider_id: str,
) -> ChainConfigSpec:
    """Drop every entry referencing ``provider_id``."""
    new: ChainConfigSpec = {}
    for chain, entries in config.items():
        kept = [e for e in entries if e.provider != provider_id]
        if kept:
            new[chain] = kept
    return new
