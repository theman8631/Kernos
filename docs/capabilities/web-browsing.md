# Web Browsing & Search

Kernos can browse the web and search for information. Two capabilities work together:

## Web Browser (Playwright, in-tree)

A pre-installed, universal capability. Available in every context space.

When the user asks you to search for something, look something up, or find current information — use this. Navigate to a relevant site or search engine (e.g., google.com), read the page with the `markdown` tool, and answer the question. You can read any page on the internet, including JavaScript-heavy sites like Notion and modern SPAs.

### Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| goto | read | Navigate to a URL |
| markdown | read | Get page content as markdown (accepts optional URL — navigate and read in one call) |
| links | read | Extract all links from current page |
| semantic_tree | read | Get DOM structure for AI reasoning (accessibility tree) |
| interactiveElements | read | List forms, buttons, inputs (accessibility-tree filter) |
| structuredData | read | Extract JSON-LD, OpenGraph, Twitter Card, meta tags |
| evaluate | soft_write | Run JavaScript on the page |

### Tips

- Use `markdown` with a URL parameter to navigate and read in one step
- For search: navigate to google.com, read results, then navigate to relevant pages
- `semantic_tree` is useful for understanding page structure before interacting
- `evaluate` is gated (soft_write) since it can modify page state

### Requirements

Playwright Python package (installed with Kernos) plus a browser. Real Chrome is detected and used by default when present on the host; bundled Chromium is the documented fallback (run `playwright install chromium` in the Kernos venv if missing). See `docs/architecture/browser.md` for backend internals and failure modes.

### Persistent profile and login

The browser uses a persistent profile at `data/browser-profile/` so cookies and login state carry across Kernos restarts. To log into a site (Notion, GitHub, Reddit, anything that requires authentication) before Kernos starts using it:

```
python -m kernos.browser login --url https://www.notion.so
```

The command opens a real browser window against the persistent profile. Authenticate manually; closing the window persists the session for future Kernos runs. Run `python -m kernos.browser info` to confirm the profile location and channel selection. Run `python -m kernos.browser --help` for the full subcommand list.

**Security note:** the profile holds session cookies for everything you log into. Anyone with read access to `data/browser-profile/` has the equivalent of your logged-in browser for those sites. Don't share that directory and don't log into accounts whose compromise would be unacceptable from a host-readable file. See `docs/architecture/browser.md` for the full threat-surface discussion.

## Web Search (Brave Search)

Structured web search via the Brave Search API. Returns titles, URLs, and snippets. Complements the browser: search finds the right page, browser reads it in depth.

### Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| brave_web_search | read | Search the web, get titles/URLs/snippets |
| brave_local_search | read | Search for local businesses/places |

### Setup

Requires `BRAVE_API_KEY` in environment. Not universal — must be activated per-space or connected at runtime.
