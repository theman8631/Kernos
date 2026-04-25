# Model registry

A canonical source for per-model metadata in Kernos. Pulled from a community-maintained upstream catalog, with a per-install overlay for findings the upstream doesn't track.

Sources:
- **Upstream:** `BerriAI/litellm` `model_prices_and_context_window.json` — about 2700 models, fields covering context limits, pricing, modality, and capability flags. Maintained constantly because LiteLLM is widely used.
- **Overlay:** `data/models/overlay.yaml` (per-install). Overrides upstream values per model name, plus a Kernos-specific block for fields upstream doesn't carry (effective context ceiling on the user's chosen endpoint, deprecation flags, free-form notes).

The merged catalog is exposed as `kernos.models.load_catalog()` returning `ModelCard` objects. Used today by the setup CLI to annotate the model picker; designed to feed downstream features (context-window-aware dispatch, deprecation warnings, cost-logging accuracy).

## File layout

```
data/models/
├── litellm.json       cached upstream snapshot (gitignored)
└── overlay.yaml       per-install corrections (gitignored)
```

A sample overlay is shipped at `kernos/models/overlay.sample.yaml`. Copy it into the data directory and edit per-model.

## Refresh path

Three ways to pull a new upstream snapshot:

1. **Auto-refresh on setup entry.** When the setup CLI's model-picker subflow opens, it calls `load_catalog(auto_refresh_if_stale=True)`. If the cache is older than 24 hours, it fetches a new copy. New upstream models become discoverable in the picker without a Kernos code change. This is the design intent — pinned with a comment in `kernos/setup/console.py` so it isn't quietly removed.

2. **Manual refresh.** `python -m kernos.models` from inside the Kernos venv. Use this between releases when you want fresh metadata without entering the setup flow.

3. **Programmatic refresh.** `refresh_catalog()` in `kernos/models/catalog.py`. Raises on network error so the caller can fall back to cached.

Refresh failures (offline, network blocked, malformed upstream payload) never throw past `load_catalog`. Failures surface as warnings on the result; cached data is used. The catalog is non-essential to Kernos's runtime: an empty catalog produces "configured-but-unknown" warnings and nothing else.

## ModelCard contract

```python
@dataclass(frozen=True)
class ModelCard:
    name: str
    provider: str
    mode: str                                  # chat | completion | image | embedding
    max_input_tokens: int | None
    max_output_tokens: int | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    supports_function_calling: bool
    supports_vision: bool
    supports_response_schema: bool
    supports_prompt_caching: bool
    kernos_notes: str
    kernos_effective_max_input_tokens: int | None
    kernos_deprecated: bool
    source: str                                # litellm | litellm+overlay | overlay-only
```

`effective_max_input_tokens` (property): returns `kernos_effective_max_input_tokens` if set, else `max_input_tokens`. Downstream context-window logic should query this rather than `max_input_tokens` directly so per-install corrections take effect.

## Overlay use cases

Three legitimate reasons to add an overlay entry:

1. **Effective ceiling differs from marketing.** The chatgpt.com consumer backend rejects payloads above ~400K tokens for GPT-5.5, even though the marketing limit is higher. The overlay records the working ceiling so dispatch logic can skip cleanly.

2. **The model isn't in upstream yet.** New models trail the LiteLLM catalog by hours to days. If you need a model right now, set its fields in the overlay and the catalog accessor will synthesize a card from the overlay alone (`source: overlay-only`).

3. **Operator wants a model deprecated locally.** Set `kernos_deprecated: true` and the setup picker will mark it. Downstream features can choose to skip deprecated entries.

## What's intentionally out

- No vendor-specific scraping. Each model's metadata comes through LiteLLM (or the overlay). We don't poll OpenAI's `/models`, Anthropic's docs, etc., for catalog purposes — providers' `/models` endpoints are still used for live validation in the setup flow, but that's a different layer.
- No price-aware routing. Catalog has pricing fields; nothing uses them yet. Fine to leave as latent capability.
- No automatic deprecation rewrites. If a configured model is removed upstream, the load result surfaces a warning; the operator decides what to do.

## Future hooks

The catalog is the data plane for several future features. Each is independent of the others; the catalog ships first so they can build on stable ground.

- **Context-window-aware dispatch.** Pre-flight skip when payload won't fit. Uses `effective_max_input_tokens`.
- **Deprecation warnings at startup.** Surface a non-fatal warning when chain build sees a configured model marked `kernos_deprecated` or absent from the catalog entirely.
- **Cost logging accuracy.** Replace hardcoded per-provider price tables with catalog values.
