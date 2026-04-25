"""Catalog loader: LiteLLM upstream + per-install overlay.

Two artifacts under `data/models/`:
- `litellm.json` — cached upstream snapshot. Refreshed on demand or
  automatically when the setup CLI's model-picker subflow opens.
- `overlay.yaml` — per-install corrections. Each entry overrides
  upstream fields for one model name; a Kernos-specific block carries
  values upstream doesn't track (effective context ceiling on a
  consumer backend, deprecation flags the user knows about, etc.).

The accessor returns merged ModelCard objects: upstream values with
overlay applied on top. Configured-but-unknown models surface as a
warning in the load result so callers can decide whether to halt or
proceed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LITELLM_CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Where Kernos stores the catalog. Caller resolves the data dir; default
# matches the rest of the project (`data/` under the repo root or
# KERNOS_DATA_DIR if set).
DEFAULT_DATA_DIR = Path(os.getenv("KERNOS_DATA_DIR", "./data"))

# How long the cached litellm.json stays warm before auto-refresh-on-
# setup-entry would fetch a new one. The setup CLI bypasses this with an
# unconditional refresh; this just bounds programmatic refresh-on-need.
DEFAULT_FRESHNESS_SECONDS = 24 * 60 * 60  # one day


@dataclass(frozen=True)
class OverlayEntry:
    """Per-install correction for one model.

    Fields here mirror a subset of LiteLLM's keys plus a Kernos-specific
    block. None means "do not override; defer to upstream".
    """

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    supports_function_calling: bool | None = None
    supports_vision: bool | None = None
    supports_response_schema: bool | None = None
    supports_prompt_caching: bool | None = None
    # Kernos-specific extras that upstream does not track.
    kernos_notes: str = ""
    kernos_effective_max_input_tokens: int | None = None
    kernos_deprecated: bool = False


@dataclass(frozen=True)
class ModelCard:
    """Merged metadata for a single model, after overlay."""

    name: str
    provider: str = ""
    mode: str = ""
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    supports_function_calling: bool = False
    supports_vision: bool = False
    supports_response_schema: bool = False
    supports_prompt_caching: bool = False
    # Kernos-specific extras propagated from overlay.
    kernos_notes: str = ""
    kernos_effective_max_input_tokens: int | None = None
    kernos_deprecated: bool = False
    # Source bookkeeping so downstream code can tell "from catalog" from
    # "synthesized because user has it configured but it's not in the
    # upstream registry".
    source: str = "litellm"

    @property
    def effective_max_input_tokens(self) -> int | None:
        """Honour the Kernos-specific override when present.

        Effective ceiling reflects what the model can actually handle on
        the install's chosen endpoint, which can be lower than the
        marketing limit. Used by downstream context-window-aware
        dispatch logic.
        """
        if self.kernos_effective_max_input_tokens is not None:
            return self.kernos_effective_max_input_tokens
        return self.max_input_tokens


@dataclass
class CatalogLoadResult:
    """Outcome of a load_catalog call."""

    cards: dict[str, ModelCard]
    warnings: list[str] = field(default_factory=list)
    upstream_age_seconds: float | None = None
    overlay_path: Path | None = None
    catalog_path: Path | None = None


def _catalog_dir(data_dir: Path | str | None = None) -> Path:
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    return base / "models"


def _catalog_path(data_dir: Path | str | None = None) -> Path:
    return _catalog_dir(data_dir) / "litellm.json"


def _overlay_path(data_dir: Path | str | None = None) -> Path:
    return _catalog_dir(data_dir) / "overlay.yaml"


def refresh_catalog(
    *,
    data_dir: Path | str | None = None,
    timeout_seconds: float = 30.0,
    url: str = LITELLM_CATALOG_URL,
    min_entries: int = 100,
) -> Path:
    """Fetch the upstream LiteLLM catalog and write it to the cache path.

    Returns the path it was written to. Raises on network error so the
    caller can decide whether to fall back to a cached copy or halt.

    `min_entries` guards against the upstream returning a partial /
    error / empty document. Real LiteLLM catalogs have thousands of
    entries; the default 100 is a generous floor. Tests can lower it.
    """
    path = _catalog_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("MODEL_CATALOG_REFRESH: url=%s timeout=%.0fs", url, timeout_seconds)
    with httpx.Client(timeout=timeout_seconds) as client:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
        # Validate it is JSON before clobbering the cache.
        parsed = resp.json()
        if not isinstance(parsed, dict) or len(parsed) < min_entries:
            raise RuntimeError(
                f"Refused to overwrite catalog: upstream payload looks "
                f"malformed (parsed type={type(parsed).__name__}, "
                f"len={len(parsed) if hasattr(parsed, '__len__') else 'n/a'})"
            )
        path.write_text(resp.text)
    logger.info("MODEL_CATALOG_REFRESH_OK: wrote=%s entries=%d", path, len(parsed))
    return path


def load_catalog(
    *,
    data_dir: Path | str | None = None,
    configured_model_names: list[str] | None = None,
    auto_refresh_if_stale: bool = False,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> CatalogLoadResult:
    """Load the merged catalog: cached LiteLLM JSON + per-install overlay.

    Behaviours:
    - If the cache is missing entirely, attempt one refresh; if that
      fails, return an empty catalog with a warning.
    - If `auto_refresh_if_stale` is True and the cache is older than
      `freshness_seconds`, refresh first. Setup CLI passes True so the
      model-picker always sees current data (with a comment marking the
      intent). Programmatic code paths default to False.
    - For each configured model name not present in the merged catalog,
      append a warning. Caller decides whether to halt or continue.
    """
    catalog_path = _catalog_path(data_dir)
    overlay_path = _overlay_path(data_dir)
    warnings: list[str] = []

    # Stage 1: ensure cache exists / is fresh enough.
    age_seconds: float | None = None
    if catalog_path.exists():
        age_seconds = time.time() - catalog_path.stat().st_mtime
        if auto_refresh_if_stale and age_seconds > freshness_seconds:
            try:
                refresh_catalog(data_dir=data_dir)
                age_seconds = time.time() - catalog_path.stat().st_mtime
            except Exception as exc:
                warnings.append(
                    f"Catalog refresh failed; using cached copy "
                    f"(age={int(age_seconds)}s): {exc}"
                )
    else:
        try:
            refresh_catalog(data_dir=data_dir)
            age_seconds = 0.0
        except Exception as exc:
            warnings.append(
                f"No cached catalog and refresh failed; "
                f"registry will be empty: {exc}"
            )

    # Stage 2: parse cached upstream.
    raw: dict[str, Any] = {}
    if catalog_path.exists():
        try:
            raw = json.loads(catalog_path.read_text())
            # LiteLLM's first key is `sample_spec`, a documentation
            # template, not a real model. Drop it.
            raw.pop("sample_spec", None)
        except Exception as exc:
            warnings.append(f"Failed to parse cached catalog: {exc}")
            raw = {}

    # Stage 3: parse overlay.
    overlay: dict[str, OverlayEntry] = _load_overlay(overlay_path, warnings)

    # Stage 4: merge into ModelCard objects.
    cards: dict[str, ModelCard] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        cards[name] = _merge_card(name, entry, overlay.get(name))

    # Stage 5: synthesize cards for overlay-only models (e.g., a model
    # the install has configured that upstream doesn't list).
    for name, ov in overlay.items():
        if name not in cards:
            cards[name] = _synthesize_from_overlay(name, ov)

    # Stage 6: warn about configured-but-unknown models.
    if configured_model_names:
        for name in configured_model_names:
            if name not in cards:
                warnings.append(
                    f"Configured model {name!r} not found in catalog "
                    f"or overlay; capability metadata unavailable"
                )

    return CatalogLoadResult(
        cards=cards,
        warnings=warnings,
        upstream_age_seconds=age_seconds,
        overlay_path=overlay_path if overlay_path.exists() else None,
        catalog_path=catalog_path if catalog_path.exists() else None,
    )


def _merge_card(
    name: str,
    upstream: dict[str, Any],
    overlay: OverlayEntry | None,
) -> ModelCard:
    """Combine one upstream entry with its overlay (if any) into a ModelCard."""
    def pick(field_name: str, default: Any = None) -> Any:
        if overlay is not None:
            ov_val = getattr(overlay, field_name, None)
            if ov_val is not None:
                return ov_val
        return upstream.get(field_name, default)

    return ModelCard(
        name=name,
        provider=upstream.get("litellm_provider", ""),
        mode=upstream.get("mode", ""),
        max_input_tokens=pick("max_input_tokens"),
        max_output_tokens=pick("max_output_tokens"),
        input_cost_per_token=pick("input_cost_per_token"),
        output_cost_per_token=pick("output_cost_per_token"),
        supports_function_calling=bool(pick("supports_function_calling", False)),
        supports_vision=bool(pick("supports_vision", False)),
        supports_response_schema=bool(pick("supports_response_schema", False)),
        supports_prompt_caching=bool(pick("supports_prompt_caching", False)),
        kernos_notes=overlay.kernos_notes if overlay else "",
        kernos_effective_max_input_tokens=(
            overlay.kernos_effective_max_input_tokens if overlay else None
        ),
        kernos_deprecated=bool(overlay.kernos_deprecated) if overlay else False,
        source="litellm+overlay" if overlay else "litellm",
    )


def _synthesize_from_overlay(name: str, ov: OverlayEntry) -> ModelCard:
    """Build a card from overlay alone (model not in upstream catalog).

    Used for installs that have configured a model upstream doesn't
    track yet — e.g., GPT-5.5 on the consumer backend, which LiteLLM
    has not added at the time of writing.
    """
    return ModelCard(
        name=name,
        max_input_tokens=ov.max_input_tokens,
        max_output_tokens=ov.max_output_tokens,
        input_cost_per_token=ov.input_cost_per_token,
        output_cost_per_token=ov.output_cost_per_token,
        supports_function_calling=bool(ov.supports_function_calling),
        supports_vision=bool(ov.supports_vision),
        supports_response_schema=bool(ov.supports_response_schema),
        supports_prompt_caching=bool(ov.supports_prompt_caching),
        kernos_notes=ov.kernos_notes,
        kernos_effective_max_input_tokens=ov.kernos_effective_max_input_tokens,
        kernos_deprecated=bool(ov.kernos_deprecated),
        source="overlay-only",
    )


def _load_overlay(
    path: Path,
    warnings: list[str],
) -> dict[str, OverlayEntry]:
    """Parse the overlay file. Tolerant of YAML being unavailable.

    YAML is optional: if PyYAML isn't installed we fall back to JSON
    parsing of the same path. Most overlay files will be small and
    JSON-compatible.
    """
    if not path.exists():
        return {}

    text = path.read_text()
    parsed: dict[str, Any] = {}
    try:
        import yaml  # type: ignore[import-not-found]
        parsed = yaml.safe_load(text) or {}
    except ImportError:
        try:
            parsed = json.loads(text)
        except Exception as exc:
            warnings.append(
                f"Overlay file present but PyYAML unavailable and "
                f"file is not JSON: {exc}"
            )
            return {}
    except Exception as exc:
        warnings.append(f"Failed to parse overlay file: {exc}")
        return {}

    if not isinstance(parsed, dict):
        warnings.append(
            f"Overlay must be a mapping of model name to fields; "
            f"got {type(parsed).__name__}"
        )
        return {}

    out: dict[str, OverlayEntry] = {}
    for name, fields in parsed.items():
        if not isinstance(fields, dict):
            warnings.append(
                f"Overlay entry {name!r} is not a mapping; skipping"
            )
            continue
        out[name] = OverlayEntry(
            max_input_tokens=fields.get("max_input_tokens"),
            max_output_tokens=fields.get("max_output_tokens"),
            input_cost_per_token=fields.get("input_cost_per_token"),
            output_cost_per_token=fields.get("output_cost_per_token"),
            supports_function_calling=fields.get("supports_function_calling"),
            supports_vision=fields.get("supports_vision"),
            supports_response_schema=fields.get("supports_response_schema"),
            supports_prompt_caching=fields.get("supports_prompt_caching"),
            kernos_notes=fields.get("kernos_notes", "") or "",
            kernos_effective_max_input_tokens=fields.get(
                "kernos_effective_max_input_tokens"
            ),
            kernos_deprecated=bool(fields.get("kernos_deprecated", False)),
        )
    return out
