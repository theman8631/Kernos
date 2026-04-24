"""Extractor-layer tests for the in-tree browser MCP server.

These do not spawn Chromium; they exercise the HTML / accessibility-tree
handling that kernos/browser/extractors.py performs post-Playwright.
"""

from kernos.browser.extractors import (
    INTERACTIVE_ROLES,
    collect_interactive_elements,
    extract_links,
    extract_structured_data,
    simplify_accessibility_tree,
)


def test_extract_links_dedupes_and_drops_javascript_hrefs():
    html = """
    <html><body>
      <a href="https://a.example">First</a>
      <a href="https://a.example">First</a>
      <a href="https://b.example/x">Second</a>
      <a href="javascript:void(0)">Noop</a>
      <a href="">Empty</a>
    </body></html>
    """
    links = extract_links(html)
    hrefs = [l["href"] for l in links]
    assert hrefs == ["https://a.example", "https://b.example/x"]


def test_extract_structured_data_reads_json_ld_and_og():
    html = """
    <html><head>
      <title>Test Page</title>
      <meta property="og:title" content="OG Title">
      <meta property="og:type" content="article">
      <meta name="twitter:card" content="summary">
      <meta name="description" content="short blurb">
      <link rel="canonical" href="https://example.com/canonical">
      <script type="application/ld+json">{"@type":"Article","name":"Hi"}</script>
      <script type="application/ld+json">not-json</script>
    </head></html>
    """
    data = extract_structured_data(html)
    assert data["title"] == "Test Page"
    assert data["canonical"] == "https://example.com/canonical"
    assert data["open_graph"] == {"title": "OG Title", "type": "article"}
    assert data["twitter"] == {"card": "summary"}
    assert data["meta"]["description"] == "short blurb"
    assert data["json_ld"] == [{"@type": "Article", "name": "Hi"}]


def test_simplify_accessibility_tree_collapses_passthrough_wrappers():
    tree = {
        "role": "generic",
        "name": "",
        "children": [
            {"role": "heading", "name": "Title", "level": 1, "children": []},
        ],
    }
    simplified = simplify_accessibility_tree(tree)
    # Generic wrapper with one child should hoist the child up.
    assert simplified == {"role": "heading", "name": "Title", "level": 1}


def test_simplify_accessibility_tree_prunes_empty_generics():
    tree = {
        "role": "document",
        "name": "Page",
        "children": [
            {"role": "generic", "name": "", "children": []},
            {"role": "paragraph", "name": "Hello", "children": []},
        ],
    }
    simplified = simplify_accessibility_tree(tree)
    assert simplified["role"] == "document"
    assert simplified["children"] == [{"role": "paragraph", "name": "Hello"}]


def test_collect_interactive_elements_filters_by_role():
    tree = {
        "role": "document",
        "name": "Page",
        "children": [
            {"role": "button", "name": "Save", "children": []},
            {"role": "paragraph", "name": "Just text", "children": []},
            {"role": "textbox", "name": "Email", "value": "me@x.com", "children": []},
            {"role": "link", "name": "Home", "children": []},
        ],
    }
    elements = collect_interactive_elements(tree)
    roles = [e["role"] for e in elements]
    assert roles == ["button", "textbox", "link"]
    textbox = next(e for e in elements if e["role"] == "textbox")
    assert textbox["value"] == "me@x.com"


def test_collect_interactive_elements_recurses():
    tree = {
        "role": "document",
        "children": [
            {
                "role": "form",
                "name": "Signup",
                "children": [
                    {"role": "textbox", "name": "Email", "children": []},
                    {"role": "button", "name": "Submit", "children": []},
                ],
            },
        ],
    }
    elements = collect_interactive_elements(tree)
    assert {e["role"] for e in elements} == {"textbox", "button"}


def test_interactive_roles_covers_common_forms():
    assert "button" in INTERACTIVE_ROLES
    assert "textbox" in INTERACTIVE_ROLES
    assert "checkbox" in INTERACTIVE_ROLES
    assert "radio" in INTERACTIVE_ROLES
    assert "link" in INTERACTIVE_ROLES
