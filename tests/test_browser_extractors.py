"""Extractor-layer tests for the in-tree browser MCP server.

These do not spawn Chromium; they exercise the HTML / accessibility-tree
handling that kernos/browser/extractors.py performs post-Playwright.
"""

from kernos.browser.extractors import (
    INTERACTIVE_ROLES,
    cdp_ax_nodes_to_tree,
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


def test_cdp_ax_nodes_to_tree_rebuilds_hierarchy():
    # CDP's Accessibility.getFullAXTree returns a flat list with childIds;
    # cdp_ax_nodes_to_tree should reassemble the nested structure.
    nodes = [
        {
            "nodeId": "1",
            "role": {"value": "RootWebArea"},
            "name": {"value": "Test Page"},
            "childIds": ["2", "3"],
        },
        {
            "nodeId": "2",
            "role": {"value": "heading"},
            "name": {"value": "Title"},
            "properties": [{"name": "level", "value": {"value": 1}}],
            "parentId": "1",
            "childIds": [],
        },
        {
            "nodeId": "3",
            "role": {"value": "button"},
            "name": {"value": "Save"},
            "parentId": "1",
            "childIds": [],
            "properties": [{"name": "focusable", "value": {"value": True}}],
        },
    ]
    tree = cdp_ax_nodes_to_tree(nodes)
    assert tree["role"] == "RootWebArea"
    assert tree["name"] == "Test Page"
    assert len(tree["children"]) == 2
    heading = tree["children"][0]
    assert heading["role"] == "heading"
    assert heading["name"] == "Title"
    assert heading["level"] == 1
    button = tree["children"][1]
    assert button["role"] == "button"
    assert button["focusable"] is True


def test_cdp_ax_nodes_to_tree_handles_empty_and_missing_children():
    # Missing child IDs shouldn't crash — they get skipped.
    nodes = [
        {
            "nodeId": "1",
            "role": {"value": "RootWebArea"},
            "name": {"value": "Page"},
            "childIds": ["missing", "2"],
        },
        {
            "nodeId": "2",
            "role": {"value": "paragraph"},
            "name": {"value": "Hello"},
            "parentId": "1",
        },
    ]
    tree = cdp_ax_nodes_to_tree(nodes)
    assert len(tree["children"]) == 1
    assert tree["children"][0]["role"] == "paragraph"

    assert cdp_ax_nodes_to_tree([]) is None


def test_cdp_ax_nodes_to_tree_collapses_ignored_nodes():
    # `Ignored` nodes hoist their children up to the next visible ancestor.
    nodes = [
        {
            "nodeId": "1",
            "role": {"value": "RootWebArea"},
            "name": {"value": "Page"},
            "childIds": ["2"],
        },
        {
            "nodeId": "2",
            "role": {"value": "Ignored"},
            "name": {"value": ""},
            "parentId": "1",
            "childIds": ["3"],
        },
        {
            "nodeId": "3",
            "role": {"value": "link"},
            "name": {"value": "Click"},
            "parentId": "2",
        },
    ]
    tree = cdp_ax_nodes_to_tree(nodes)
    assert tree["role"] == "RootWebArea"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["role"] == "link"
    assert tree["children"][0]["name"] == "Click"


def test_collect_interactive_elements_excludes_root_webarea():
    # RootWebArea is technically "focusable" in the AX tree, but it's not
    # a user-interactive element. The filter must exclude it.
    tree = {
        "role": "RootWebArea",
        "name": "Page",
        "focusable": True,
        "children": [
            {"role": "button", "name": "Save", "focusable": True},
        ],
    }
    elements = collect_interactive_elements(tree)
    roles = [e["role"] for e in elements]
    assert "RootWebArea" not in roles
    assert "button" in roles
