# Updating the LLM Benchmark Snapshot

## What the snapshot is

`config/llm_benchmark_snapshot.json` is a small JSON file mapping each provider in the registry to its current "best intelligence" and "fast / efficient" model ids. When a user runs `kernos setup llm` and adds a provider, Kernos reads this file to suggest sensible default models for the `primary` and `cheap` chain tiers. The user accepts or overrides via the numbered menu.

## When to update it

Update the snapshot when a provider ships a newer model that should be the default for either tier. The update is a one-line JSON edit and does not require a release.

## How to update it

1. Edit `config/llm_benchmark_snapshot.json` directly.
2. Bump `snapshot_date` to today.
3. Update the relevant provider's `primary` or `cheap` model id to the new recommendation.
4. Commit.

That is the entire update path. Forkers who maintain their own distributions can edit this file for their own deployments.

## Who reads the snapshot

Exactly one code path:

* `kernos setup llm` — provider-add flow (`kernos/setup/console.py` → `kernos/setup/benchmark_snapshot.py`).

No runtime code path reads the snapshot. After setup completes, `ChainConfig` is the source of truth for which models Kernos uses. Existing installs are unaffected by snapshot edits; new installs (and re-runs of `kernos setup llm`) pick up the new recommendations.

This is a contract: if you find any other import of `kernos.setup.benchmark_snapshot` or any other reader of `config/llm_benchmark_snapshot.json`, that's a bug.

## Schema

```json
{
  "schema_version": 1,
  "snapshot_date": "YYYY-MM-DD",
  "providers": {
    "<provider_id>": {
      "primary": "<model id for main intelligence>",
      "cheap":   "<model id for fast / efficient>"
    },
    ...
  }
}
```

`provider_id` must match a provider in `kernos/setup/provider_registry.py`. Unknown providers are ignored by the reader (no error), so leaving placeholder entries is harmless.

## What if a recommended model is no longer available?

If the snapshot recommends a model that the provider's `/models` endpoint no longer returns, the setup flow detects this, surfaces a note, and falls through to the full model list for the user to pick from manually. Setup still works — the snapshot is a convenience, not a dependency.
