"""Setup-CLI catalog annotation: model lines show capability flags."""

from kernos.models.catalog import ModelCard
from kernos.setup.console import _annotate_model, _compact_int


def test_compact_int_renders_thousands_and_millions():
    assert _compact_int(500) == "500"
    assert _compact_int(128_000) == "128K"
    assert _compact_int(1_000_000) == "1M"
    assert _compact_int(2_500_000) == "2M"


def test_annotate_model_returns_bare_name_when_catalog_missing_entry():
    assert _annotate_model("never-heard-of-it", {}) == "never-heard-of-it"


def test_annotate_model_shows_context_window():
    catalog = {
        "gpt-4o": ModelCard(name="gpt-4o", max_input_tokens=128_000),
    }
    line = _annotate_model("gpt-4o", catalog)
    assert "ctx 128K" in line


def test_annotate_model_lists_capability_flags():
    catalog = {
        "gpt-4o": ModelCard(
            name="gpt-4o",
            max_input_tokens=128_000,
            supports_vision=True,
            supports_function_calling=True,
            supports_response_schema=True,
        ),
    }
    line = _annotate_model("gpt-4o", catalog)
    assert "vision" in line
    assert "fn-calling" in line
    assert "schema" in line


def test_annotate_model_marks_deprecated():
    catalog = {
        "old-model": ModelCard(
            name="old-model", max_input_tokens=8_000, kernos_deprecated=True
        ),
    }
    line = _annotate_model("old-model", catalog)
    assert "deprecated" in line


def test_annotate_model_uses_kernos_effective_max_when_present():
    """Override carries through — what the user actually gets matters more
    than the marketing limit."""
    catalog = {
        "gpt-5.5": ModelCard(
            name="gpt-5.5",
            max_input_tokens=400_000,
            kernos_effective_max_input_tokens=400_000,
        ),
    }
    line = _annotate_model("gpt-5.5", catalog)
    assert "ctx 400K" in line


def test_annotate_model_returns_bare_name_when_card_has_no_capabilities():
    catalog = {
        "minimal": ModelCard(name="minimal"),  # no fields set
    }
    assert _annotate_model("minimal", catalog) == "minimal"
