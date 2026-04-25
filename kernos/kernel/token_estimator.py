"""Conservative payload token estimator for the chain dispatcher's
pre-flight context-window skip.

Takes the system prompt, messages, and tool schemas the dispatcher is
about to send and returns an integer estimate of input tokens. Used by
the chain dispatcher to decide whether each chain entry's effective
context window can hold the payload before the request is sent.

Goal: an over-estimate biased on the safe side. The dispatcher
combines this with a configurable safety margin (default ten percent)
so estimator inaccuracy cannot push a payload past the model's actual
limit. We are not trying to predict actual tiktoken counts; we are
trying to make decisions that don't fail downstream.

Heuristic:
- English prose averages around four characters per token. We use
  that for the system prompt and message text content.
- Tool schemas tend to be denser (JSON, identifiers). We charge a
  flat fixed overhead per tool plus three characters per token to
  bias high.
- Structured message content (dicts and lists) is JSON-ified before
  the chars-per-token rule applies, so nested content does not get
  silently undercounted.

The accuracy bar is set by the safety margin downstream. As long as
this function is within ten percent of reality, the dispatcher's
decisions are sound. Empirically the heuristic over-estimates English
prose by a small amount which is the desired direction.
"""

from __future__ import annotations

import json
from typing import Any

# Chars per token for free-form text. Slightly conservative; English
# prose is closer to 3.8-4.2.
_CHARS_PER_TOKEN_TEXT = 4

# Chars per token for dense JSON / tool-schema content. JSON
# identifiers and punctuation push the ratio lower; bias high so we
# do not undercount tool-heavy turns.
_CHARS_PER_TOKEN_JSON = 3

# Fixed overhead per tool schema for protocol tokens (name, type
# wrapper, JSON-schema scaffolding) the chars-per-token rule does not
# capture cleanly.
_PER_TOOL_OVERHEAD_TOKENS = 16


def estimate_tokens(
    *,
    system: str | list[dict] | None,
    messages: list[dict] | None,
    tools: list[dict] | None,
) -> int:
    """Return a conservative input-token estimate for a payload.

    None inputs are treated as empty. Messages can carry string content
    or a list of content blocks. Tools are JSON-ified before counting
    so nested schemas do not slip through.
    """
    chars_text = 0
    chars_json = 0

    chars_text += _system_chars(system)

    for msg in messages or []:
        chars_text += _message_chars(msg)

    tool_count = 0
    for tool in tools or []:
        tool_count += 1
        try:
            chars_json += len(json.dumps(tool, ensure_ascii=False))
        except (TypeError, ValueError):
            chars_json += len(str(tool))

    text_tokens = chars_text // _CHARS_PER_TOKEN_TEXT
    json_tokens = chars_json // _CHARS_PER_TOKEN_JSON
    overhead_tokens = tool_count * _PER_TOOL_OVERHEAD_TOKENS

    return text_tokens + json_tokens + overhead_tokens


def _system_chars(system: str | list[dict] | None) -> int:
    if system is None:
        return 0
    if isinstance(system, str):
        return len(system)
    total = 0
    for block in system:
        if isinstance(block, dict):
            text = block.get("text", "")
            if isinstance(text, str):
                total += len(text)
    return total


def _message_chars(msg: dict) -> int:
    """Char count for one message, handling string and structured content."""
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                # Defensive: serialize anything unexpected.
                total += len(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                total += len(block.get("text", "") or "")
            elif btype == "tool_use":
                # Tool-use blocks carry an input dict that needs counting.
                try:
                    total += len(
                        json.dumps(block.get("input") or {}, ensure_ascii=False)
                    )
                except (TypeError, ValueError):
                    total += len(str(block.get("input")))
                total += len(block.get("name", "") or "")
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    total += len(inner)
                else:
                    try:
                        total += len(json.dumps(inner, ensure_ascii=False))
                    except (TypeError, ValueError):
                        total += len(str(inner))
            else:
                # Unknown block type: serialize the whole thing.
                try:
                    total += len(json.dumps(block, ensure_ascii=False))
                except (TypeError, ValueError):
                    total += len(str(block))
        return total
    if content is None:
        return 0
    # Defensive: if the content is something else, render it.
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(content))
