"""HTML → structured extractions used by the browser MCP server.

Kept separate from server.py so the extraction logic is testable without
spinning up a real browser. Each function takes HTML (or an accessibility
snapshot) and returns a JSON-serializable result.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "combobox",
    "searchbox",
    "checkbox",
    "radio",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "switch",
    "slider",
    "spinbutton",
    "option",
}


def extract_links(html: str, base_url: str = "") -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("javascript:"):
            continue
        text = " ".join((a.get_text() or "").split())
        key = f"{href}|{text}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "href": href})
    return out


def extract_structured_data(html: str) -> dict[str, Any]:
    """JSON-LD, OpenGraph, Twitter Cards, essential meta tags.

    Schema.org microdata is intentionally skipped in v1; add if a caller
    actually needs it.
    """
    soup = BeautifulSoup(html, "html.parser")

    json_ld: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            json_ld.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.debug("skipping unparseable JSON-LD block")

    open_graph: dict[str, str] = {}
    twitter: dict[str, str] = {}
    meta: dict[str, str] = {}

    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or "").strip().lower()
        name = (tag.get("name") or "").strip().lower()
        content = (tag.get("content") or "").strip()
        if not content:
            continue
        if prop.startswith("og:"):
            open_graph[prop[3:]] = content
        elif name.startswith("twitter:"):
            twitter[name[8:]] = content
        elif name in {"description", "author", "keywords", "viewport"}:
            meta[name] = content

    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else ""

    canonical = ""
    link_canonical = soup.find("link", rel="canonical")
    if link_canonical and link_canonical.get("href"):
        canonical = link_canonical["href"].strip()

    return {
        "title": title,
        "canonical": canonical,
        "meta": meta,
        "open_graph": open_graph,
        "twitter": twitter,
        "json_ld": json_ld,
    }


def simplify_accessibility_tree(node: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim Playwright's accessibility snapshot to fields an LLM cares about.

    The raw snapshot is verbose. We drop empty-child arms, collapse
    generic/none roles, and keep role/name/value/level plus children.
    """
    if node is None:
        return None
    role = node.get("role") or ""
    name = (node.get("name") or "").strip()
    children_raw = node.get("children") or []

    simplified_children: list[dict[str, Any]] = []
    for child in children_raw:
        s = simplify_accessibility_tree(child)
        if s is not None:
            simplified_children.append(s)

    if role in {"generic", "none", ""} and not name:
        # Collapse a passthrough wrapper — hoist its children up the tree.
        if len(simplified_children) == 1:
            return simplified_children[0]
        if not simplified_children:
            return None

    result: dict[str, Any] = {"role": role or "generic"}
    if name:
        result["name"] = name
    value = node.get("value")
    if value:
        result["value"] = value
    level = node.get("level")
    if level:
        result["level"] = level
    if simplified_children:
        result["children"] = simplified_children
    return result


def collect_interactive_elements(
    node: dict[str, Any] | None,
    out: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten the accessibility tree to a list of interactive nodes.

    An element is interactive if its role is in INTERACTIVE_ROLES or the
    snapshot reports a focusable property.
    """
    if out is None:
        out = []
    if node is None:
        return out
    role = node.get("role") or ""
    focusable = bool(node.get("focusable")) or bool(node.get("focused"))
    if role in INTERACTIVE_ROLES or (focusable and role not in {"generic", "none", ""}):
        entry: dict[str, Any] = {"role": role or "generic"}
        name = (node.get("name") or "").strip()
        if name:
            entry["name"] = name
        value = node.get("value")
        if value:
            entry["value"] = value
        if node.get("disabled"):
            entry["disabled"] = True
        if node.get("checked") is not None:
            entry["checked"] = node.get("checked")
        out.append(entry)
    for child in node.get("children") or []:
        collect_interactive_elements(child, out)
    return out
