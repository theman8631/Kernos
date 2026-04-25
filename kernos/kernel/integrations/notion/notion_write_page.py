"""notion_write_page: append markdown content to a Notion page.

Q7 follow-on to STOCK-INTEGRATIONS-NOTION. Same shape as
notion_read_page; the read tool's structure is the in-tree reference.
Closes the harness-audit Notion-write loop — paste-back was the
workaround; this tool is the proper path.

Output shape:

    {
        "page_id":               "<id>",
        "appended_block_count":  <int>,
    }

Or on error:

    {"error": "<message>"}

The tool calls Notion's `PATCH /v1/blocks/{page_id}/children` to
append blocks to the target page. The markdown-to-blocks converter
handles a minimal but sufficient subset:

  - paragraphs, blank-line-separated
  - headings (#, ##, ### — and #### maps to heading_3)
  - bulleted lists (-, *)
  - numbered lists (1. 2. 3. — any digit prefix)
  - blockquotes (>)
  - fenced code blocks (```language ... ```)

Inline markdown (bold, italic, code, links) is NOT parsed in v1;
text content is sent as a single rich-text run with no annotations.
Inline parsing is a follow-on if a real workflow needs it; the
harness-audit case (response sections, plain prose plus structure)
does not.
"""

from __future__ import annotations

import re

import httpx

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def execute(input_data, context):
    payload = input_data or {}
    page_id = (payload.get("page_id") or "").strip()
    markdown = payload.get("markdown") or ""
    if not page_id:
        return {"error": "page_id is required"}
    if not markdown.strip():
        return {"error": "markdown is required and must contain non-whitespace content"}

    try:
        credential = context.credentials.get()
    except Exception as exc:
        return {"error": f"credential not available: {exc}"}

    blocks = _markdown_to_blocks(markdown)
    if not blocks:
        return {"error": "markdown produced no renderable blocks"}

    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.patch(
                f"{NOTION_API}/blocks/{page_id}/children",
                headers=headers,
                json={"children": blocks},
            )
            if resp.status_code >= 400:
                return _format_api_error(resp)
    except httpx.HTTPError as exc:
        return {"error": f"Notion request failed: {exc}"}

    return {
        "page_id": page_id,
        "appended_block_count": len(blocks),
    }


def _format_api_error(resp):
    try:
        body = resp.json()
        message = body.get("message") or body.get("code") or ""
    except Exception:
        message = resp.text[:200]
    return {
        "error": (
            f"Notion API returned {resp.status_code}"
            f"{': ' + message if message else ''}"
        ),
    }


# ---------------------------------------------------------------------------
# Markdown → Notion blocks
# ---------------------------------------------------------------------------


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_FENCE_RE = re.compile(r"^```(\S*)\s*$")


def _markdown_to_blocks(markdown: str) -> list[dict]:
    """Convert a markdown string into a list of Notion block dicts.

    The converter is line-oriented: it walks lines, accumulates
    paragraphs across consecutive non-empty lines, opens fenced code
    blocks until the closing fence, and emits one block per recognised
    structural element.
    """
    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    paragraph: list[str] = []

    def _flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(p.strip() for p in paragraph).strip()
        if text:
            blocks.append(_paragraph(text))
        paragraph.clear()

    while i < len(lines):
        line = lines[i]
        # Fenced code block — opening fence
        fence = _FENCE_RE.match(line)
        if fence:
            _flush_paragraph()
            language = fence.group(1).strip().lower() or "plain text"
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            # Skip the closing fence (or end-of-input).
            if i < len(lines):
                i += 1
            blocks.append(_code_block("\n".join(code_lines), language))
            continue

        if not line.strip():
            _flush_paragraph()
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            _flush_paragraph()
            level = len(heading.group(1))
            text = heading.group(2).strip()
            blocks.append(_heading(text, level))
            i += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            _flush_paragraph()
            blocks.append(_bullet(bullet.group(1).strip()))
            i += 1
            continue

        numbered = _NUMBERED_RE.match(line)
        if numbered:
            _flush_paragraph()
            blocks.append(_numbered(numbered.group(1).strip()))
            i += 1
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            _flush_paragraph()
            blocks.append(_quote(quote.group(1).strip()))
            i += 1
            continue

        # Default: accumulate into a paragraph.
        paragraph.append(line)
        i += 1

    _flush_paragraph()
    return blocks


def _rich_text(text: str) -> list[dict]:
    """Single rich-text run with no inline annotations.

    Notion's per-run text limit is 2000 characters; we split long
    strings rather than truncating so nothing silently disappears.
    """
    if not text:
        return []
    chunks = [text[j : j + 2000] for j in range(0, len(text), 2000)]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def _heading(text: str, level: int) -> dict:
    # Notion supports h1, h2, h3. Levels deeper than 3 collapse to h3.
    block_type = f"heading_{min(level, 3)}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _rich_text(text)},
    }


def _bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(text)},
    }


def _numbered(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": _rich_text(text)},
    }


def _quote(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": _rich_text(text)},
    }


def _code_block(text: str, language: str) -> dict:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": _rich_text(text),
            "language": language,
        },
    }
