"""Model registry — community-sourced catalog plus per-install overlay.

Source: BerriAI/litellm `model_prices_and_context_window.json`. Pulled
at install or refresh time, cached locally. Per-install overlay file
records Kernos-specific findings the upstream catalog won't have
(e.g., consumer-backend effective context limits).

See docs/architecture/model-registry.md for the contract.
"""

from kernos.models.catalog import (
    LITELLM_CATALOG_URL,
    ModelCard,
    OverlayEntry,
    load_catalog,
    refresh_catalog,
)

__all__ = [
    "LITELLM_CATALOG_URL",
    "ModelCard",
    "OverlayEntry",
    "load_catalog",
    "refresh_catalog",
]
