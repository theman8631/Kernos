"""Benchmark-snapshot reader — SETUP-TIME ONLY.

Loads ``config/llm_benchmark_snapshot.json`` and surfaces per-provider,
per-chain-tier model recommendations.

**Contract:** this module is imported exactly once, from the
``kernos setup llm`` console flow. No runtime code path (server startup,
reasoning loop, handler pipeline, friction observer, anything) reads the
snapshot. If you find a new import of this module outside ``kernos/setup/``,
that's a contract violation — file an issue or kick back.

The snapshot is data, not code. Update path for forkers: edit the JSON and
restart. New installs get the new recommendations; existing installs are
unaffected (their configured models persist in ``ChainConfig``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("config/llm_benchmark_snapshot.json")


class BenchmarkSnapshotError(Exception):
    """Snapshot file is missing or malformed."""


def load_snapshot(path: Path | None = None) -> dict:
    """Load and minimally validate the snapshot JSON. Returns the parsed dict."""
    snapshot_path = path or _DEFAULT_PATH
    if not snapshot_path.exists():
        raise BenchmarkSnapshotError(
            f"Benchmark snapshot missing at {snapshot_path}. "
            "Setup cannot proceed without it."
        )
    try:
        data = json.loads(snapshot_path.read_text())
    except json.JSONDecodeError as exc:
        raise BenchmarkSnapshotError(
            f"Benchmark snapshot at {snapshot_path} is malformed JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or "providers" not in data:
        raise BenchmarkSnapshotError(
            f"Benchmark snapshot at {snapshot_path} has no 'providers' section."
        )
    return data


def recommended_models(
    provider_id: str,
    *,
    path: Path | None = None,
) -> dict[str, str]:
    """Return ``{chain_tier: model_id}`` recommendations for ``provider_id``.

    Chain tiers currently surfaced at setup: ``primary`` (S-class, best
    intelligence) and ``cheap`` (B-class, fast/efficient). ``simple``
    inherits from ``cheap`` post-setup — not asked at initial setup.

    Returns an empty dict if the provider has no snapshot entry (this just
    means the user has to pick a model from the /models list manually —
    setup still works).
    """
    data = load_snapshot(path)
    providers = data.get("providers", {})
    entry = providers.get(provider_id, {})
    if not isinstance(entry, dict):
        logger.warning(
            "Snapshot entry for provider %r is not a dict; ignoring.", provider_id,
        )
        return {}
    return {
        tier: model
        for tier, model in entry.items()
        if isinstance(model, str) and model
    }
