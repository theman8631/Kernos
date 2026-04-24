"""In-tree browser MCP server.

Replaces Lightpanda with a Playwright-backed implementation that executes
JavaScript correctly on modern sites. Preserves the seven tool schemas
callers depended on: goto, markdown, links, semantic_tree, interactiveElements,
structuredData, evaluate.

See docs/architecture/browser.md for backend rationale and failure modes.
"""
