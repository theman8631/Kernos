"""Tests for the pre-flight token estimator used by chain dispatch."""

from kernos.kernel.token_estimator import estimate_tokens


def test_empty_inputs_estimate_zero():
    assert estimate_tokens(system=None, messages=None, tools=None) == 0
    assert estimate_tokens(system="", messages=[], tools=[]) == 0


def test_system_string_estimated_at_four_chars_per_token():
    # 100 chars / 4 = 25 tokens.
    text = "x" * 100
    assert estimate_tokens(system=text, messages=[], tools=[]) == 25


def test_system_list_uses_text_field_of_each_block():
    sys = [{"text": "abcd" * 10}, {"text": "ef" * 10}]
    # 40 + 20 = 60 chars / 4 = 15 tokens.
    assert estimate_tokens(system=sys, messages=[], tools=[]) == 15


def test_string_message_content_counted():
    msg = [{"role": "user", "content": "x" * 80}]
    # 80 / 4 = 20 tokens.
    assert estimate_tokens(system=None, messages=msg, tools=[]) == 20


def test_structured_text_block_counted():
    msg = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "x" * 40}],
        }
    ]
    assert estimate_tokens(system=None, messages=msg, tools=[]) == 10


def test_tool_use_block_counts_input_dict_plus_name():
    msg = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "list_events",  # 11 chars
                    "input": {"date": "2026-04-25"},  # JSON-ified
                }
            ],
        }
    ]
    # tool_use uses text rate (4 chars/token).
    # JSON of input: {"date": "2026-04-25"} = 22 chars; name = 11.
    # 33 / 4 = 8 tokens.
    n = estimate_tokens(system=None, messages=msg, tools=[])
    assert n >= 6  # bounded — exact int depends on json formatting


def test_tool_result_string_content_counted():
    msg = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "x" * 60},
            ],
        }
    ]
    assert estimate_tokens(system=None, messages=msg, tools=[]) == 15


def test_tool_result_structured_content_counted_via_json():
    msg = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "x",
                    "content": {"a": "b" * 30},
                },
            ],
        }
    ]
    n = estimate_tokens(system=None, messages=msg, tools=[])
    # Just a sanity bound — content is JSON-serialized first.
    assert n >= 8


def test_tools_use_dense_json_rate_plus_overhead():
    # Two tool schemas, ~120 chars each when JSON-ified (rough).
    tool = {
        "name": "list_events",
        "description": "List calendar events for a date",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
    }
    no_tools = estimate_tokens(system=None, messages=[], tools=[])
    with_tools = estimate_tokens(system=None, messages=[], tools=[tool, tool])
    # Each tool contributes its JSON / 3 plus 16-token fixed overhead.
    # Two tools should add at minimum 2 * 16 = 32 tokens beyond the base.
    assert with_tools - no_tools >= 32


def test_estimator_biases_high_on_dense_payloads():
    """Tool-heavy payload should never under-estimate vs the chars-per-token
    rule applied to the prose-only equivalent."""
    big_tool = {
        "name": "x",
        "description": "y" * 200,
        "input_schema": {"type": "object", "properties": {}},
    }
    with_tools = estimate_tokens(system=None, messages=[], tools=[big_tool])
    # Same JSON treated as message text would be 4-chars-per-token; tool
    # path uses 3-chars-per-token plus overhead, so it must be larger.
    import json
    text_equiv_chars = len(json.dumps(big_tool, ensure_ascii=False))
    text_equiv_tokens = text_equiv_chars // 4
    assert with_tools > text_equiv_tokens


def test_combined_payload_components_add_independently():
    """system + messages + tools should be additive."""
    sys = "x" * 100  # 25 tokens
    msg = [{"role": "user", "content": "y" * 200}]  # 50 tokens
    tool = {"name": "x", "input_schema": {"type": "object"}}
    only_sys = estimate_tokens(system=sys, messages=[], tools=[])
    only_msg = estimate_tokens(system=None, messages=msg, tools=[])
    only_tool = estimate_tokens(system=None, messages=[], tools=[tool])
    combined = estimate_tokens(system=sys, messages=msg, tools=[tool])
    assert combined == only_sys + only_msg + only_tool
