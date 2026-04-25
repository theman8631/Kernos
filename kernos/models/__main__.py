"""Manual catalog refresh: `python -m kernos.models`.

Pulls the upstream LiteLLM model_prices_and_context_window JSON and
writes it to data/models/litellm.json. Use this between releases to
pick up new models without reinstalling Kernos. The setup CLI's
model-picker also auto-refreshes when stale, so this is mostly for
operators who want to force a refresh on demand.
"""

from __future__ import annotations

import logging
import sys

from kernos.models.catalog import refresh_catalog


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        path = refresh_catalog()
    except Exception as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 1
    print(f"catalog refreshed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
