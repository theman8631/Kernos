"""Pre-flight context-window skip in the chain dispatcher.

Exercises the new logic in ReasoningService._call_chain that consults
the merged catalog before each provider call, skipping entries whose
effective ceiling cannot fit the estimated payload.
"""

from dataclasses import dataclass
from typing import Any

import pytest

from kernos.kernel.exceptions import ChainPayloadTooLarge, LLMChainExhausted
from kernos.models.catalog import ModelCard
from kernos.providers.base import ChainEntry, Provider, ProviderResponse


# ---------------------------------------------------------------------------
# Test scaffolding: a tiny in-process provider that lets the test
# observe whether complete() was called and short-circuits the actual
# transport.
# ---------------------------------------------------------------------------


class FakeProvider(Provider):
    def __init__(self, *, name: str, response_text: str = "ok"):
        self.provider_name = name
        self.calls: list[dict] = []
        self._response_text = response_text
        self._trace = None

    async def complete(  # type: ignore[override]
        self,
        model,
        system,
        messages,
        tools,
        max_tokens,
        output_schema=None,
        conversation_id="",
    ) -> ProviderResponse:
        self.calls.append({"model": model, "tokens": max_tokens})
        return ProviderResponse(
            content=[],
            stop_reason="end_turn",
            input_tokens=0,
            output_tokens=0,
        )


def _make_service_with_chain(monkeypatch, entries, catalog: dict[str, ModelCard]):
    """Construct a minimal ReasoningService with the given chain.

    Patches the catalog accessor so the test does not touch the real
    registry on disk.
    """
    from kernos.kernel.reasoning import ReasoningService

    chains = {"primary": entries, "lightweight": entries}

    # Stub the events / mcp / audit dependencies; the chain walk does
    # not exercise them under these tests.
    rs = ReasoningService.__new__(ReasoningService)
    rs._chains = chains
    rs._provider = entries[0].provider
    rs._events = None
    rs._mcp = None
    rs._audit = None
    rs._retrieval = None
    rs._files = None
    rs._registry = None
    rs._state = None
    rs._channel_registry = None
    rs._trigger_store = None
    rs._handler = None
    rs._canvas = None
    rs._gate = None
    rs._pending_actions = {}
    rs._conflict_raised_this_turn = False
    rs._tools_changed = False
    rs._loaded_tools = {}
    rs._turn_tool_trace = []
    rs._last_real_input_tokens = {}
    rs._catalog_cards = dict(catalog)
    rs._unknown_model_warned = set()
    return rs


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_all_entries_fit_uses_first_entry(monkeypatch):
    """Tiny payload → entry one called, no skips."""
    p1 = FakeProvider(name="p1")
    p2 = FakeProvider(name="p2")
    chain = [
        ChainEntry(provider=p1, model="model-a"),
        ChainEntry(provider=p2, model="model-b"),
    ]
    catalog = {
        "model-a": ModelCard(name="model-a", max_input_tokens=100_000),
        "model-b": ModelCard(name="model-b", max_input_tokens=200_000),
    }
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    await rs._call_chain(
        chain_name="primary",
        system="hello",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=64,
    )
    assert len(p1.calls) == 1
    assert len(p2.calls) == 0


async def test_payload_exceeds_first_entry_skips_to_fallback(monkeypatch):
    """Between-tier payload → primary skipped, fallback used."""
    p1 = FakeProvider(name="p1")
    p2 = FakeProvider(name="p2")
    chain = [
        ChainEntry(provider=p1, model="small"),
        ChainEntry(provider=p2, model="big"),
    ]
    catalog = {
        # 8K ceiling minus 10% margin → threshold ~7200. We will send
        # a payload around 25K characters (~6250 tokens of text alone).
        "small": ModelCard(name="small", max_input_tokens=8_000),
        # Plenty of room.
        "big": ModelCard(name="big", max_input_tokens=200_000),
    }
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    big_user = "x" * 40_000  # 10_000 tokens at 4 chars/token
    await rs._call_chain(
        chain_name="primary",
        system="",
        messages=[{"role": "user", "content": big_user}],
        tools=[],
        max_tokens=64,
    )
    assert p1.calls == []  # skipped
    assert len(p2.calls) == 1


async def test_payload_exceeds_all_entries_raises_payload_too_large(monkeypatch):
    """All-too-small payload → distinct ChainPayloadTooLarge, no calls."""
    p1 = FakeProvider(name="p1")
    p2 = FakeProvider(name="p2")
    chain = [
        ChainEntry(provider=p1, model="small"),
        ChainEntry(provider=p2, model="medium"),
    ]
    catalog = {
        "small": ModelCard(name="small", max_input_tokens=4_000),
        "medium": ModelCard(name="medium", max_input_tokens=10_000),
    }
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    huge = "x" * 80_000  # 20_000 tokens
    with pytest.raises(ChainPayloadTooLarge) as excinfo:
        await rs._call_chain(
            chain_name="primary",
            system="",
            messages=[{"role": "user", "content": huge}],
            tools=[],
            max_tokens=64,
        )
    assert p1.calls == []
    assert p2.calls == []
    err = excinfo.value
    # Exception carries the diagnostic numbers needed for surfacing.
    assert err.estimated_tokens >= 20_000
    assert err.largest_ceiling == 10_000
    # Distinct from the existing exhaustion exception.
    assert not isinstance(err, LLMChainExhausted)


async def test_unknown_model_routes_normally_no_skip(monkeypatch):
    """Configured model with no catalog card → not skipped on context grounds."""
    p1 = FakeProvider(name="p1")
    chain = [ChainEntry(provider=p1, model="never-registered")]
    catalog: dict = {}  # nothing in registry
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    huge = "x" * 80_000
    # Should call the provider normally; no skip, no exception.
    await rs._call_chain(
        chain_name="primary",
        system="",
        messages=[{"role": "user", "content": huge}],
        tools=[],
        max_tokens=64,
    )
    assert len(p1.calls) == 1


async def test_safety_margin_keeps_boundary_payloads_on_the_safe_side(monkeypatch):
    """Payload right at the ceiling minus margin should still fit."""
    p1 = FakeProvider(name="p1")
    chain = [ChainEntry(provider=p1, model="exact")]
    # Ceiling 10K, margin 10% → threshold 9000.
    # 35_000 chars / 4 = 8750 tokens, comfortably under.
    catalog = {"exact": ModelCard(name="exact", max_input_tokens=10_000)}
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    payload = "x" * 35_000
    await rs._call_chain(
        chain_name="primary",
        system="",
        messages=[{"role": "user", "content": payload}],
        tools=[],
        max_tokens=64,
    )
    assert len(p1.calls) == 1


async def test_kernos_effective_max_overrides_marketing_limit(monkeypatch):
    """When the overlay sets a tighter effective ceiling, the dispatcher
    honours it even when the marketing limit would have allowed the call."""
    p1 = FakeProvider(name="p1")
    p2 = FakeProvider(name="p2")
    chain = [
        ChainEntry(provider=p1, model="overrated"),
        ChainEntry(provider=p2, model="generous"),
    ]
    catalog = {
        # Marketing says 50K, but the install knows it really only
        # handles 5K reliably. Effective ceiling wins.
        "overrated": ModelCard(
            name="overrated",
            max_input_tokens=50_000,
            kernos_effective_max_input_tokens=5_000,
        ),
        "generous": ModelCard(name="generous", max_input_tokens=200_000),
    }
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    payload = "x" * 40_000  # 10K tokens
    await rs._call_chain(
        chain_name="primary",
        system="",
        messages=[{"role": "user", "content": payload}],
        tools=[],
        max_tokens=64,
    )
    assert p1.calls == []  # effective ceiling skipped it
    assert len(p2.calls) == 1


async def test_safety_margin_env_override_applies(monkeypatch):
    """KERNOS_CONTEXT_SAFETY_MARGIN env var changes the threshold."""
    p1 = FakeProvider(name="p1")
    chain = [ChainEntry(provider=p1, model="tight")]
    catalog = {"tight": ModelCard(name="tight", max_input_tokens=10_000)}
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    # 25K chars / 4 = 6250 tokens. Default 10% margin → threshold 9000;
    # would pass. With a 50% margin → threshold 5000; would skip.
    monkeypatch.setenv("KERNOS_CONTEXT_SAFETY_MARGIN", "0.5")
    payload = "x" * 25_000
    with pytest.raises(ChainPayloadTooLarge):
        await rs._call_chain(
            chain_name="primary",
            system="",
            messages=[{"role": "user", "content": payload}],
            tools=[],
            max_tokens=64,
        )
    assert p1.calls == []


async def test_chain_payload_too_large_distinct_from_exhausted_when_some_calls_made(
    monkeypatch,
):
    """If at least one entry was called and failed, raise the existing
    LLMChainExhausted, not ChainPayloadTooLarge."""
    from kernos.kernel.exceptions import ReasoningProviderError

    class FailingProvider(FakeProvider):
        async def complete(self, **kwargs):  # type: ignore[override]
            self.calls.append(kwargs)
            raise ReasoningProviderError("upstream is down")

    p1 = FailingProvider(name="p1")
    p2 = FailingProvider(name="p2")
    chain = [
        ChainEntry(provider=p1, model="model-a"),
        ChainEntry(provider=p2, model="model-b"),
    ]
    catalog = {
        "model-a": ModelCard(name="model-a", max_input_tokens=100_000),
        "model-b": ModelCard(name="model-b", max_input_tokens=200_000),
    }
    rs = _make_service_with_chain(monkeypatch, chain, catalog)

    with pytest.raises(LLMChainExhausted):
        await rs._call_chain(
            chain_name="primary",
            system="",
            messages=[{"role": "user", "content": "small"}],
            tools=[],
            max_tokens=64,
        )
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1
