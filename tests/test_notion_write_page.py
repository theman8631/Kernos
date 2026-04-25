"""Tests for notion_write_page (Q7 follow-on).

Pure-logic tests for the markdown-to-blocks converter; execute()
tests with httpx mocked.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent
    / "kernos" / "kernel" / "integrations" / "notion"
)
sys.path.insert(0, str(_INTEGRATION_DIR))

import notion_write_page as tool  # noqa: E402

sys.path.remove(str(_INTEGRATION_DIR))


# ---------------------------------------------------------------------------
# Markdown → blocks
# ---------------------------------------------------------------------------


def _types(blocks):
    return [b["type"] for b in blocks]


def test_paragraph_emits_single_paragraph_block():
    blocks = tool._markdown_to_blocks("This is a paragraph.")
    assert _types(blocks) == ["paragraph"]
    rt = blocks[0]["paragraph"]["rich_text"]
    assert rt[0]["text"]["content"] == "This is a paragraph."


def test_consecutive_lines_merge_into_one_paragraph():
    md = "First line.\nSecond line.\nThird line."
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == ["paragraph"]
    text = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "First line" in text and "Second line" in text and "Third line" in text


def test_blank_line_separates_paragraphs():
    md = "First paragraph.\n\nSecond paragraph."
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == ["paragraph", "paragraph"]


def test_headings_emit_correct_levels():
    md = "# H1\n\n## H2\n\n### H3"
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == ["heading_1", "heading_2", "heading_3"]


def test_headings_deeper_than_three_collapse_to_h3():
    blocks = tool._markdown_to_blocks("#### Four levels")
    assert _types(blocks) == ["heading_3"]


def test_bulleted_lists():
    md = "- one\n- two\n- three"
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == [
        "bulleted_list_item",
        "bulleted_list_item",
        "bulleted_list_item",
    ]


def test_numbered_lists_with_paren_or_dot():
    md = "1. one\n2) two\n3. three"
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == [
        "numbered_list_item",
        "numbered_list_item",
        "numbered_list_item",
    ]


def test_blockquote():
    blocks = tool._markdown_to_blocks("> quote text")
    assert _types(blocks) == ["quote"]
    rt = blocks[0]["quote"]["rich_text"]
    assert rt[0]["text"]["content"] == "quote text"


def test_fenced_code_block_with_language():
    md = "```python\nprint('hi')\n```"
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == ["code"]
    code = blocks[0]["code"]
    assert code["language"] == "python"
    assert "print('hi')" in code["rich_text"][0]["text"]["content"]


def test_fenced_code_block_without_language_defaults_to_plain_text():
    md = "```\nliteral text\n```"
    blocks = tool._markdown_to_blocks(md)
    assert blocks[0]["code"]["language"] == "plain text"


def test_fenced_code_block_with_no_closing_fence_consumes_remainder():
    """Robustness against malformed input — a missing closing fence
    consumes lines until end-of-input rather than crashing."""
    md = "```python\nrunaway code\n"
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == ["code"]


def test_mixed_structural_elements():
    md = (
        "# Title\n\n"
        "Intro paragraph.\n\n"
        "- bullet one\n- bullet two\n\n"
        "1. step one\n2. step two\n\n"
        "> a quote\n\n"
        "Final paragraph."
    )
    blocks = tool._markdown_to_blocks(md)
    assert _types(blocks) == [
        "heading_1",
        "paragraph",
        "bulleted_list_item",
        "bulleted_list_item",
        "numbered_list_item",
        "numbered_list_item",
        "quote",
        "paragraph",
    ]


def test_long_paragraph_chunked_into_2000_char_runs():
    """Notion's per-run text limit is 2000 chars; longer paragraphs
    get chunked rather than truncated."""
    long_text = "x" * 5000
    blocks = tool._markdown_to_blocks(long_text)
    rt = blocks[0]["paragraph"]["rich_text"]
    # Three chunks (2000 + 2000 + 1000).
    assert len(rt) == 3
    total = sum(len(r["text"]["content"]) for r in rt)
    assert total == 5000


def test_empty_markdown_produces_no_blocks():
    assert tool._markdown_to_blocks("") == []
    assert tool._markdown_to_blocks("   \n\n  ") == []


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return str(self._body)


class _Client:
    def __init__(self, *, status=200, body=None):
        self._status = status
        self._body = body or {}
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def patch(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json})
        return _Resp(self._status, self._body)


def _fake_credential():
    return SimpleNamespace(token="secret_xyz")


def _fake_context():
    creds = SimpleNamespace(get=lambda: _fake_credential())
    return SimpleNamespace(credentials=creds)


def test_execute_happy_path(monkeypatch):
    fake_client = _Client(body={"results": []})
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake_client)
    result = tool.execute(
        {"page_id": "abc-123", "markdown": "# Heading\n\nA paragraph."},
        _fake_context(),
    )
    assert result["page_id"] == "abc-123"
    assert result["appended_block_count"] == 2
    # Notion API call shape sanity.
    call = fake_client.calls[0]
    assert call["url"].endswith("/blocks/abc-123/children")
    assert call["headers"]["Authorization"] == "Bearer secret_xyz"
    assert call["headers"]["Notion-Version"]
    assert len(call["json"]["children"]) == 2


def test_execute_missing_page_id():
    result = tool.execute({"markdown": "x"}, _fake_context())
    assert result == {"error": "page_id is required"}


def test_execute_missing_markdown():
    result = tool.execute({"page_id": "abc"}, _fake_context())
    assert "markdown is required" in result["error"]


def test_execute_whitespace_only_markdown():
    result = tool.execute({"page_id": "abc", "markdown": "   \n\n   "}, _fake_context())
    assert "markdown is required" in result["error"]


def test_execute_credential_unavailable():
    bad_context = SimpleNamespace(
        credentials=SimpleNamespace(
            get=lambda: (_ for _ in ()).throw(RuntimeError("no creds")),
        ),
    )
    result = tool.execute(
        {"page_id": "abc", "markdown": "x"}, bad_context,
    )
    assert "credential not available" in result["error"]


def test_execute_api_error_returns_clean_error(monkeypatch):
    monkeypatch.setattr(
        tool.httpx, "Client",
        lambda *a, **kw: _Client(
            status=403, body={"code": "unauthorized", "message": "API token does not have access"},
        ),
    )
    result = tool.execute(
        {"page_id": "abc", "markdown": "# X"}, _fake_context(),
    )
    assert "Notion API returned 403" in result["error"]
    assert "API token does not have access" in result["error"]


def test_execute_network_error(monkeypatch):
    class _ErrClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def patch(self, *a, **kw): raise tool.httpx.HTTPError("connection refused")

    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: _ErrClient())
    result = tool.execute(
        {"page_id": "abc", "markdown": "x"}, _fake_context(),
    )
    assert "Notion request failed" in result["error"]
    assert "connection refused" in result["error"]
