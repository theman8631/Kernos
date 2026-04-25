"""notion_read_page: read a Notion page as markdown.

The first stock instance of WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE.
Demonstrates the worked-example shape: declares service_id and
authority, consumes context.credentials.get() to fetch the invoking
member's token, calls the Notion API, returns markdown.

Output shape:

    {
        "page_id":  "<id>",
        "title":    "<page title or empty>",
        "markdown": "<rendered page content>",
    }

Or on error:

    {"error": "<message>"}

Errors arrive as ordinary dict returns rather than exceptions. The
workshop primitive's enforcement layer catches credential / authority
issues before this code runs; runtime errors here are typically API
failures (Notion rate limit, bad page id, etc.).
"""

from __future__ import annotations

import re

import httpx

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def execute(input_data, context):
    page_id = (input_data or {}).get("page_id", "").strip()
    if not page_id:
        return {"error": "page_id is required"}

    try:
        credential = context.credentials.get()
    except Exception as exc:
        return {"error": f"credential not available: {exc}"}

    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Notion-Version": NOTION_VERSION,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            page_resp = client.get(f"{NOTION_API}/pages/{page_id}", headers=headers)
            if page_resp.status_code >= 400:
                return _format_api_error(page_resp)
            page = page_resp.json()

            blocks_resp = client.get(
                f"{NOTION_API}/blocks/{page_id}/children",
                headers=headers,
                params={"page_size": 100},
            )
            if blocks_resp.status_code >= 400:
                return _format_api_error(blocks_resp)
            blocks = blocks_resp.json().get("results", [])
    except httpx.HTTPError as exc:
        return {"error": f"Notion request failed: {exc}"}

    return {
        "page_id": page_id,
        "title": _extract_title(page),
        "markdown": _blocks_to_markdown(blocks),
    }


def _format_api_error(resp):
    try:
        payload = resp.json()
        message = payload.get("message") or payload.get("code") or ""
    except Exception:
        message = resp.text[:200]
    return {
        "error": (
            f"Notion API returned {resp.status_code}"
            f"{': ' + message if message else ''}"
        ),
    }


def _extract_title(page):
    """Best-effort title extraction from a Notion page payload."""
    properties = page.get("properties") or {}
    for value in properties.values():
        if value.get("type") == "title":
            return _rich_text_to_plain(value.get("title", []))
    return ""


def _rich_text_to_plain(rich_text):
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def _rich_text_to_markdown(rich_text):
    out = []
    for rt in rich_text or []:
        text = rt.get("plain_text", "")
        annotations = rt.get("annotations") or {}
        href = rt.get("href")
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _blocks_to_markdown(blocks):
    """Render a Notion blocks array to markdown.

    Covers the common block types: paragraph, headings, bulleted/numbered
    lists, to-do, quote, divider, code, callout. Unknown blocks render
    as a placeholder line so the agent sees their presence.
    """
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        body = block.get(btype) or {}
        text = _rich_text_to_markdown(body.get("rich_text", []))
        if btype == "paragraph":
            parts.append(text)
        elif btype == "heading_1":
            parts.append(f"# {text}")
        elif btype == "heading_2":
            parts.append(f"## {text}")
        elif btype == "heading_3":
            parts.append(f"### {text}")
        elif btype == "bulleted_list_item":
            parts.append(f"- {text}")
        elif btype == "numbered_list_item":
            parts.append(f"1. {text}")
        elif btype == "to_do":
            checked = body.get("checked", False)
            parts.append(f"- [{'x' if checked else ' '}] {text}")
        elif btype == "quote":
            parts.append(f"> {text}")
        elif btype == "divider":
            parts.append("---")
        elif btype == "code":
            language = body.get("language", "")
            parts.append(f"```{language}\n{text}\n```")
        elif btype == "callout":
            parts.append(f"> {text}")
        elif btype == "child_page":
            child_title = body.get("title", "")
            parts.append(f"[child page: {child_title}]")
        else:
            # Unknown block type — surface its existence without
            # pretending to render it.
            parts.append(f"_<{btype} block>_")
    return "\n\n".join(p for p in parts if p)
