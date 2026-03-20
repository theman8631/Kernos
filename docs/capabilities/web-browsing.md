# Web Browsing & Search

Kernos can browse the web and search for information. Two capabilities work together:

## Web Browser (Lightpanda)

A pre-installed, universal capability. Available in every context space.

When the user asks you to search for something, look something up, or find current information — use this. Navigate to a relevant site or search engine (e.g., google.com), read the page with the `markdown` tool, and answer the question. You can read any page on the internet.

### Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| goto | read | Navigate to a URL |
| markdown | read | Get page content as markdown (accepts optional URL — navigate and read in one call) |
| links | read | Extract all links from current page |
| semantic_tree | read | Get DOM structure for AI reasoning |
| interactiveElements | read | List forms, buttons, inputs |
| structuredData | read | Extract JSON-LD, OpenGraph metadata |
| evaluate | soft_write | Run JavaScript on the page |

### Tips

- Use `markdown` with a URL parameter to navigate and read in one step
- For search: navigate to google.com, read results, then navigate to relevant pages
- `semantic_tree` is useful for understanding page structure before interacting
- `evaluate` is gated (soft_write) since it can modify page state

### Requirements

Lightpanda binary at `~/bin/lightpanda` (x86_64 Linux only).

## Web Search (Brave Search)

Structured web search via the Brave Search API. Returns titles, URLs, and snippets. Complements the browser: search finds the right page, browser reads it in depth.

### Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| brave_web_search | read | Search the web, get titles/URLs/snippets |
| brave_local_search | read | Search for local businesses/places |

### Setup

Requires `BRAVE_API_KEY` in environment. Not universal — must be activated per-space or connected at runtime.
