"""Tests for the notion_read_page worked tool.

Pure-logic helpers (markdown rendering) get straight unit tests.
The execute() entry point is tested with httpx mocked so we exercise
the API-call wiring without hitting the real Notion service.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# Add the integration's directory to sys.path so we can import the
# tool by its module name. In production this happens at dispatch
# time inside _execute_service_bound_tool.
_INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "kernos" / "kernel" / "integrations" / "notion"
sys.path.insert(0, str(_INTEGRATION_DIR))

import notion_read_page as tool  # noqa: E402

sys.path.remove(str(_INTEGRATION_DIR))


# ---------------------------------------------------------------------------
# Pure-logic helpers
# ---------------------------------------------------------------------------


def test_rich_text_to_plain():
    out = tool._rich_text_to_plain([
        {"plain_text": "Hello, "},
        {"plain_text": "world"},
    ])
    assert out == "Hello, world"


def test_rich_text_to_markdown_handles_annotations():
    rt = [{
        "plain_text": "bold",
        "annotations": {"bold": True},
        "href": None,
    }]
    assert tool._rich_text_to_markdown(rt) == "**bold**"


def test_rich_text_to_markdown_handles_links():
    rt = [{
        "plain_text": "Kernos",
        "annotations": {},
        "href": "https://example.com",
    }]
    assert tool._rich_text_to_markdown(rt) == "[Kernos](https://example.com)"


def test_blocks_to_markdown_renders_headings_and_lists():
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title", "annotations": {}}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "A paragraph.", "annotations": {}}]}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "First", "annotations": {}}]}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "Second", "annotations": {}}]}},
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "checked task", "annotations": {}}], "checked": True}},
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "unchecked task", "annotations": {}}], "checked": False}},
        {"type": "divider", "divider": {}},
    ]
    md = tool._blocks_to_markdown(blocks)
    assert "# Title" in md
    assert "A paragraph." in md
    assert "- First" in md
    assert "- Second" in md
    assert "- [x] checked task" in md
    assert "- [ ] unchecked task" in md
    assert "---" in md


def test_blocks_to_markdown_renders_code_with_language():
    blocks = [{
        "type": "code",
        "code": {
            "language": "python",
            "rich_text": [{"plain_text": "print('hi')", "annotations": {}}],
        },
    }]
    md = tool._blocks_to_markdown(blocks)
    assert "```python" in md
    assert "print('hi')" in md


def test_blocks_to_markdown_surfaces_unknown_block_types():
    blocks = [{
        "type": "embed",
        "embed": {"rich_text": [{"plain_text": "x", "annotations": {}}]},
    }]
    md = tool._blocks_to_markdown(blocks)
    assert "<embed block>" in md


def test_extract_title_handles_typical_page():
    page = {
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "My Page"}],
            },
            "Status": {"type": "status", "status": {"name": "Done"}},
        },
    }
    assert tool._extract_title(page) == "My Page"


def test_extract_title_returns_empty_when_no_title_property():
    assert tool._extract_title({"properties": {}}) == ""


# ---------------------------------------------------------------------------
# execute() entry point — mocked transport
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return str(self._body)


class _Client:
    def __init__(self, *, page, blocks, page_status=200, blocks_status=200):
        self._page = page
        self._blocks = blocks
        self._page_status = page_status
        self._blocks_status = blocks_status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None, params=None):
        if "/pages/" in url:
            return _Resp(self._page_status, self._page)
        if "/blocks/" in url:
            return _Resp(self._blocks_status, {"results": self._blocks})
        return _Resp(404, {"message": "unknown route"})


def _fake_credential():
    return SimpleNamespace(token="secret_xyz")


def _fake_context():
    creds = SimpleNamespace(get=lambda: _fake_credential())
    return SimpleNamespace(credentials=creds)


def test_execute_happy_path(monkeypatch):
    page = {
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "About"}]},
        },
    }
    blocks = [{
        "type": "heading_1",
        "heading_1": {"rich_text": [{"plain_text": "About", "annotations": {}}]},
    }, {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": "This is a page.", "annotations": {}}]},
    }]
    monkeypatch.setattr(
        tool.httpx, "Client",
        lambda *a, **kw: _Client(page=page, blocks=blocks),
    )
    result = tool.execute({"page_id": "abc-123"}, _fake_context())
    assert result["page_id"] == "abc-123"
    assert result["title"] == "About"
    assert "About" in result["markdown"]
    assert "This is a page." in result["markdown"]


def test_execute_missing_page_id():
    result = tool.execute({}, _fake_context())
    assert result == {"error": "page_id is required"}


def test_execute_credential_unavailable():
    bad_context = SimpleNamespace(
        credentials=SimpleNamespace(
            get=lambda: (_ for _ in ()).throw(RuntimeError("no creds")),
        ),
    )
    result = tool.execute({"page_id": "abc"}, bad_context)
    assert "credential not available" in result["error"]


def test_execute_api_error_returns_clean_error(monkeypatch):
    monkeypatch.setattr(
        tool.httpx, "Client",
        lambda *a, **kw: _Client(
            page={"message": "object not found", "code": "object_not_found"},
            blocks=[],
            page_status=404,
        ),
    )
    result = tool.execute({"page_id": "abc"}, _fake_context())
    assert "Notion API returned 404" in result["error"]
    assert "object not found" in result["error"]
