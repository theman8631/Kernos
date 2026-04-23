"""Credential resolution tests for the Aider builder.

Spec reference: SPEC-BUILDER-AIDER-BACKEND, Pillar 2 — credential pass-through.

Exercises ``_resolve_aider_config()`` directly. No subprocess, no network.
"""
from __future__ import annotations

import pytest

from kernos.kernel.builders.aider import _resolve_aider_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start each test from a clean env for the relevant vars."""
    for name in (
        "KERNOS_LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AIDER_MODEL",
        "AIDER_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


class TestAnthropicPassThrough:
    """Pillar 2 expected behavior #1: Anthropic pass-through works.

    Aider now mirrors Kernos's primary Anthropic model
    (``AnthropicProvider.main_model``) instead of using aider's ``sonnet``
    alias. Keeps the two agents on the same model family without a
    separate config knob.
    """

    def _primary_anthropic_model(self):
        from kernos.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider.main_model

    def test_anthropic_with_key_mirrors_primary(self, monkeypatch):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        # Mirrors Kernos's primary AnthropicProvider.main_model
        assert cfg["model"] == self._primary_anthropic_model()
        # Sanity: it's a claude-* model (Kernos's primary family)
        assert cfg["model"].startswith("claude-")
        assert cfg["env_updates"] == {"ANTHROPIC_API_KEY": "sk-ant-test-value"}

    def test_default_provider_is_anthropic(self, monkeypatch):
        # Unset KERNOS_LLM_PROVIDER — default must be anthropic
        monkeypatch.delenv("KERNOS_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == self._primary_anthropic_model()


class TestAnthropicMissingKey:
    """Pillar 2 expected behavior #4: missing credentials fail clean."""

    def test_anthropic_without_key_errors(self, monkeypatch):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        cfg = _resolve_aider_config()
        assert cfg["model"] == ""
        assert cfg["error"] is not None
        assert "ANTHROPIC_API_KEY" in cfg["error"]


class TestCodexWithoutOverride:
    """Pillar 2 expected behavior #2: Codex without override fails clean."""

    def test_codex_without_aider_model_errors(self, monkeypatch):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        cfg = _resolve_aider_config()
        assert cfg["model"] == ""
        assert cfg["error"] is not None
        assert "AIDER_MODEL" in cfg["error"]
        assert "AIDER_API_KEY" in cfg["error"]
        assert "openai-codex" in cfg["error"]

    def test_ollama_without_aider_model_or_ollama_model_errors(self, monkeypatch):
        """With ollama provider and NEITHER AIDER_MODEL nor OLLAMA_MODEL set,
        the adapter has no way to mirror Kernos's primary — must error."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        cfg = _resolve_aider_config()
        assert cfg["model"] == ""
        assert cfg["error"] is not None
        assert "AIDER_MODEL" in cfg["error"] or "OLLAMA_MODEL" in cfg["error"]

    def test_ollama_mirrors_ollama_model_env(self, monkeypatch):
        """KERNOS_LLM_PROVIDER=ollama + OLLAMA_MODEL=foo → aider uses
        ``ollama_chat/foo`` automatically (mirrors Kernos's primary)."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "gemma4:31b-cloud")
        monkeypatch.setenv("OLLAMA_API_BASE", "https://ollama.com")
        monkeypatch.setenv("OLLAMA_API_KEY", "cloud-key")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "ollama_chat/gemma4:31b-cloud"
        assert cfg["env_updates"]["OLLAMA_API_BASE"] == "https://ollama.com"
        assert cfg["env_updates"]["OLLAMA_API_KEY"] == "cloud-key"

    def test_ollama_mirrors_with_already_prefixed_model(self, monkeypatch):
        """If OLLAMA_MODEL already has the ollama_chat/ prefix, don't double-prefix."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "ollama_chat/llama3")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "ollama_chat/llama3"


class TestAiderModelOverride:
    """Pillar 2 expected behavior #3: AIDER_MODEL override respected."""

    def test_override_with_api_key_uses_model(self, monkeypatch):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        monkeypatch.setenv("AIDER_MODEL", "gpt-4o")
        monkeypatch.setenv("AIDER_API_KEY", "sk-openai-test")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "gpt-4o"
        assert cfg["env_updates"] == {"OPENAI_API_KEY": "sk-openai-test"}

    def test_anthropic_override_model(self, monkeypatch):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("AIDER_MODEL", "claude-opus-4")
        monkeypatch.setenv("AIDER_API_KEY", "sk-ant-opus")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "claude-opus-4"
        assert cfg["env_updates"] == {"ANTHROPIC_API_KEY": "sk-ant-opus"}

    def test_ollama_local_needs_no_key(self, monkeypatch):
        """Local Ollama: no OLLAMA_* env set → empty env_updates."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("AIDER_MODEL", "ollama_chat/llama3")
        # Explicitly ensure no Ollama env is inherited
        for var in ("OLLAMA_API_BASE", "OLLAMA_BASE_URL", "OLLAMA_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "ollama_chat/llama3"
        assert cfg["env_updates"] == {}

    def test_ollama_cloud_passes_base_and_key(self, monkeypatch):
        """Cloud Ollama: OLLAMA_API_BASE + OLLAMA_API_KEY pass through."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("AIDER_MODEL", "ollama_chat/gemma:7b-cloud")
        monkeypatch.setenv("OLLAMA_API_BASE", "https://ollama.com")
        monkeypatch.setenv("OLLAMA_API_KEY", "test-cloud-key")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "ollama_chat/gemma:7b-cloud"
        assert cfg["env_updates"] == {
            "OLLAMA_API_BASE": "https://ollama.com",
            "OLLAMA_API_KEY": "test-cloud-key",
        }

    def test_ollama_translates_base_url_to_api_base(self, monkeypatch):
        """Kernos's OLLAMA_BASE_URL gets translated to litellm's OLLAMA_API_BASE."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("AIDER_MODEL", "ollama_chat/llama3-cloud")
        monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
        monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com")
        monkeypatch.setenv("OLLAMA_API_KEY", "k")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["env_updates"]["OLLAMA_API_BASE"] == "https://ollama.com"

    def test_override_reuses_existing_provider_key(self, monkeypatch):
        """AIDER_MODEL without AIDER_API_KEY: fall back to matching existing env."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-existing")
        monkeypatch.setenv("AIDER_MODEL", "claude-opus-4")  # anthropic model
        # No AIDER_API_KEY
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "claude-opus-4"
        assert cfg["env_updates"] == {"ANTHROPIC_API_KEY": "sk-ant-existing"}

    def test_override_no_key_no_matching_env_errors(self, monkeypatch):
        """AIDER_MODEL set, no AIDER_API_KEY, no matching env → clear error."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        monkeypatch.setenv("AIDER_MODEL", "gpt-4o")
        # No AIDER_API_KEY, no OPENAI_API_KEY
        cfg = _resolve_aider_config()
        assert cfg["model"] == ""
        assert cfg["error"] is not None
        assert "gpt-4o" in cfg["error"]
        assert "AIDER_API_KEY" in cfg["error"]

    def test_unknown_model_prefix_uses_generic_pass_through(self, monkeypatch):
        """Model with a prefix we don't recognize: pass AIDER_API_KEY through as-is."""
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        monkeypatch.setenv("AIDER_MODEL", "some-experimental-model")
        monkeypatch.setenv("AIDER_API_KEY", "sk-experimental")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["model"] == "some-experimental-model"
        assert cfg["env_updates"] == {"AIDER_API_KEY": "sk-experimental"}


class TestModelPrefixInference:
    """Coverage of the model-prefix → env-var mapping."""

    @pytest.mark.parametrize("model,expected_env", [
        ("sonnet", "ANTHROPIC_API_KEY"),
        ("haiku-3-5", "ANTHROPIC_API_KEY"),
        ("opus-4", "ANTHROPIC_API_KEY"),
        ("claude-3-5-sonnet-20240620", "ANTHROPIC_API_KEY"),
        ("gpt-4o", "OPENAI_API_KEY"),
        ("gpt-4.1", "OPENAI_API_KEY"),
        ("o1-mini", "OPENAI_API_KEY"),
        ("o3-large", "OPENAI_API_KEY"),
        ("4o-mini", "OPENAI_API_KEY"),
        ("gemini-pro", "GEMINI_API_KEY"),
        ("deepseek-coder", "DEEPSEEK_API_KEY"),
        ("groq-llama", "GROQ_API_KEY"),
    ])
    def test_prefix_inference(self, monkeypatch, model, expected_env):
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        monkeypatch.setenv("AIDER_MODEL", model)
        monkeypatch.setenv("AIDER_API_KEY", "test-key-value")
        cfg = _resolve_aider_config()
        assert cfg["error"] is None
        assert cfg["env_updates"] == {expected_env: "test-key-value"}
