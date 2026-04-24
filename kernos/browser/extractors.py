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


_PROPERTY_PASSTHROUGH = {"focusable", "focused", "disabled", "level", "checked"}


def cdp_ax_nodes_to_tree(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reconstruct a nested accessibility tree from CDP's flat node list.

    The Chrome DevTools Protocol `Accessibility.getFullAXTree` call returns
    a flat list where each entry carries `nodeId`, `role.value`, `name.value`,
    optional `value.value`, `properties[]`, and `childIds[]`. We walk from
    the root (no `parentId`) and produce the nested `{role, name, children, ...}`
    shape the existing simplifier and interactive-element collector expect.

    Ignored nodes (`role.value == 'Ignored'`) are skipped transparently —
    their children hoist up to whatever ancestor is next in the chain.
    """
    if not nodes:
        return None
    by_id: dict[str, dict[str, Any]] = {n["nodeId"]: n for n in nodes}

    # Identify roots — CDP sometimes emits more than one top-level tree
    # (e.g., iframe subtrees). Use the first node with no parentId in
    # the map; fall back to the first entry.
    root: dict[str, Any] | None = None
    for n in nodes:
        pid = n.get("parentId")
        if pid is None or pid not in by_id:
            root = n
            break
    if root is None:
        root = nodes[0]

    def _walk(node: dict[str, Any]) -> dict[str, Any] | None:
        role = ((node.get("role") or {}).get("value") or "").strip()
        if role.lower() == "ignored":
            # Collapse ignored nodes by returning a placeholder wrapper
            # whose children will be hoisted one level up in the caller.
            children: list[dict[str, Any]] = []
            for cid in node.get("childIds") or []:
                child = by_id.get(cid)
                if child is None:
                    continue
                walked = _walk(child)
                if walked is None:
                    continue
                # Flatten a single ignored wrapper's hoisted children
                if walked.get("role") == "__ignored_group__":
                    children.extend(walked.get("children") or [])
                else:
                    children.append(walked)
            return {"role": "__ignored_group__", "children": children} if children else None

        name = ((node.get("name") or {}).get("value") or "").strip()
        value = ((node.get("value") or {}).get("value") or "") if node.get("value") else ""

        out: dict[str, Any] = {"role": role or "generic"}
        if name:
            out["name"] = name
        if value:
            out["value"] = value

        for prop in node.get("properties") or []:
            pname = prop.get("name")
            if pname not in _PROPERTY_PASSTHROUGH:
                continue
            pvalue = (prop.get("value") or {}).get("value")
            if pvalue is None or pvalue is False:
                continue
            out[pname] = pvalue

        children: list[dict[str, Any]] = []
        for cid in node.get("childIds") or []:
            child = by_id.get(cid)
            if child is None:
                continue
            walked = _walk(child)
            if walked is None:
                continue
            if walked.get("role") == "__ignored_group__":
                children.extend(walked.get("children") or [])
            else:
                children.append(walked)
        if children:
            out["children"] = children
        return out

    tree = _walk(root)
    # Unwrap any accidental __ignored_group__ at the top.
    if tree and tree.get("role") == "__ignored_group__":
        inner = tree.get("children") or []
        return inner[0] if len(inner) == 1 else {"role": "document", "children": inner}
    return tree


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
    if role in INTERACTIVE_ROLES or (
        focusable
        and role
        not in {"generic", "none", "", "RootWebArea", "WebArea", "window", "document"}
    ):
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
