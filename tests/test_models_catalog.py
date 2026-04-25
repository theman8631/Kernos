"""Tests for kernos.models.catalog.

No network: refresh_catalog is exercised against a fake httpx response.
Load tests use small fixture catalogs written to a tmp_path data dir.
"""

import json

import pytest

from kernos.models.catalog import (
    ModelCard,
    OverlayEntry,
    _catalog_path,
    _overlay_path,
    load_catalog,
    refresh_catalog,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_catalog(data_dir, payload: dict) -> None:
    path = _catalog_path(data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _write_overlay(data_dir, content: str) -> None:
    path = _overlay_path(data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# Minimal upstream-shaped fixture matching LiteLLM's keys.
_FIXTURE_UPSTREAM = {
    "sample_spec": {"this_should_be_dropped": True},
    "gpt-4o": {
        "litellm_provider": "openai",
        "mode": "chat",
        "max_input_tokens": 128000,
        "max_output_tokens": 16384,
        "input_cost_per_token": 2.5e-06,
        "output_cost_per_token": 1e-05,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_response_schema": True,
        "supports_prompt_caching": True,
    },
    "claude-haiku-4-5": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "max_input_tokens": 200000,
        "max_output_tokens": 8192,
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "supports_function_calling": True,
        "supports_vision": True,
    },
}


# ---------------------------------------------------------------------------
# load_catalog: cached upstream is parsed and sample_spec dropped
# ---------------------------------------------------------------------------


def test_load_catalog_drops_sample_spec(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    result = load_catalog(data_dir=tmp_path)
    assert "sample_spec" not in result.cards
    assert "gpt-4o" in result.cards


def test_load_catalog_returns_cards_with_upstream_fields(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    result = load_catalog(data_dir=tmp_path)
    card = result.cards["gpt-4o"]
    assert isinstance(card, ModelCard)
    assert card.provider == "openai"
    assert card.max_input_tokens == 128000
    assert card.supports_vision is True
    assert card.source == "litellm"


# ---------------------------------------------------------------------------
# Overlay merge
# ---------------------------------------------------------------------------


def test_overlay_overrides_upstream_field(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    _write_overlay(
        tmp_path,
        json.dumps(
            {
                "gpt-4o": {
                    "max_input_tokens": 100000,  # override marketing 128k → 100k
                    "kernos_notes": "rate-limited tier on this install",
                    "kernos_effective_max_input_tokens": 90000,
                }
            }
        ),
    )
    result = load_catalog(data_dir=tmp_path)
    card = result.cards["gpt-4o"]
    assert card.max_input_tokens == 100000
    assert card.kernos_notes == "rate-limited tier on this install"
    # Effective ceiling honours the kernos-specific override.
    assert card.effective_max_input_tokens == 90000
    assert card.source == "litellm+overlay"


def test_overlay_only_synthesizes_card_for_unknown_model(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    _write_overlay(
        tmp_path,
        json.dumps(
            {
                "gpt-5.5": {
                    "max_input_tokens": 400000,
                    "kernos_effective_max_input_tokens": 400000,
                    "kernos_notes": "consumer backend",
                }
            }
        ),
    )
    result = load_catalog(data_dir=tmp_path)
    card = result.cards["gpt-5.5"]
    assert card.source == "overlay-only"
    assert card.max_input_tokens == 400000
    assert card.effective_max_input_tokens == 400000


def test_effective_max_falls_back_to_upstream_when_no_kernos_override(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    result = load_catalog(data_dir=tmp_path)
    card = result.cards["claude-haiku-4-5"]
    assert card.kernos_effective_max_input_tokens is None
    assert card.effective_max_input_tokens == 200000


# ---------------------------------------------------------------------------
# Configured-but-unknown warning
# ---------------------------------------------------------------------------


def test_load_warns_for_configured_unknown_models(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    result = load_catalog(
        data_dir=tmp_path,
        configured_model_names=["gpt-4o", "made-up-model:cloud"],
    )
    # Real model produces no warning; fictional one produces a warning.
    relevant = [w for w in result.warnings if "made-up-model:cloud" in w]
    assert len(relevant) == 1
    assert "not found" in relevant[0].lower()


def test_load_does_not_warn_when_overlay_provides_unknown_model(tmp_path):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    _write_overlay(
        tmp_path,
        json.dumps({"gpt-5.5": {"max_input_tokens": 400000}}),
    )
    result = load_catalog(
        data_dir=tmp_path, configured_model_names=["gpt-5.5"]
    )
    assert all("gpt-5.5" not in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Empty / missing cache handling
# ---------------------------------------------------------------------------


def test_load_returns_empty_with_warning_when_cache_missing_and_offline(
    tmp_path, monkeypatch
):
    """If the cache is missing and refresh fails, return empty + warn."""

    def fake_refresh(*, data_dir=None, **kwargs):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("kernos.models.catalog.refresh_catalog", fake_refresh)
    result = load_catalog(data_dir=tmp_path)
    assert result.cards == {}
    assert any("refresh failed" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# refresh_catalog (no real network)
# ---------------------------------------------------------------------------


def test_refresh_catalog_writes_payload(tmp_path, monkeypatch):
    captured: dict = {}

    class FakeResp:
        text = json.dumps(_FIXTURE_UPSTREAM)
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return _FIXTURE_UPSTREAM

    class FakeClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, follow_redirects=False):
            captured["url"] = url
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    path = refresh_catalog(
        data_dir=tmp_path,
        url="https://example.test/catalog",
        min_entries=2,
    )
    assert path.exists()
    assert captured["url"] == "https://example.test/catalog"
    # The cached file is the literal text we returned.
    written = json.loads(path.read_text())
    assert "gpt-4o" in written


def test_refresh_catalog_refuses_malformed_payload(tmp_path, monkeypatch):
    """Don't clobber the cache with garbage."""

    class FakeResp:
        text = "{}"
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {}  # too small — refuse

    class FakeClient:
        def __init__(self, *, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, follow_redirects=False):
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(RuntimeError, match="malformed"):
        refresh_catalog(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Auto-refresh-if-stale (matches setup-CLI intent)
# ---------------------------------------------------------------------------


def test_load_auto_refreshes_when_stale(tmp_path, monkeypatch):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    # Backdate the cache file so it looks stale.
    import os
    import time

    catalog_path = _catalog_path(data_dir=tmp_path)
    old_mtime = time.time() - (48 * 60 * 60)  # 48 hours old
    os.utime(catalog_path, (old_mtime, old_mtime))

    refreshed_called = {"n": 0}

    def fake_refresh(*, data_dir=None, **kwargs):
        refreshed_called["n"] += 1
        # Touch the file so age becomes fresh.
        catalog_path.touch()
        return catalog_path

    monkeypatch.setattr("kernos.models.catalog.refresh_catalog", fake_refresh)
    result = load_catalog(data_dir=tmp_path, auto_refresh_if_stale=True)
    assert refreshed_called["n"] == 1
    # Cache present; no warning about missing cache.
    assert all("missing" not in w.lower() for w in result.warnings)


def test_load_does_not_refresh_when_fresh(tmp_path, monkeypatch):
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    refreshed_called = {"n": 0}

    def fake_refresh(*, data_dir=None, **kwargs):
        refreshed_called["n"] += 1
        return _catalog_path(data_dir=data_dir)

    monkeypatch.setattr("kernos.models.catalog.refresh_catalog", fake_refresh)
    load_catalog(data_dir=tmp_path, auto_refresh_if_stale=True)
    # File was just written, so it is fresh; refresh should not have been called.
    assert refreshed_called["n"] == 0


def test_load_falls_back_to_cached_when_refresh_fails(tmp_path, monkeypatch):
    """If we have a stale cache and refresh fails, surface a warning but
    continue with cached data."""
    _write_catalog(tmp_path, _FIXTURE_UPSTREAM)
    import os
    import time

    catalog_path = _catalog_path(data_dir=tmp_path)
    old_mtime = time.time() - (48 * 60 * 60)
    os.utime(catalog_path, (old_mtime, old_mtime))

    def fake_refresh(*, data_dir=None, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("kernos.models.catalog.refresh_catalog", fake_refresh)
    result = load_catalog(data_dir=tmp_path, auto_refresh_if_stale=True)
    assert "gpt-4o" in result.cards
    assert any("refresh failed" in w.lower() for w in result.warnings)
