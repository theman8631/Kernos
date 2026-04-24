# Browser backend

Kernos's web-browser capability is served by an **in-tree Playwright-backed MCP server** (`kernos/browser/`). It replaced Lightpanda on 2026-04-24 after the previous binary's JavaScript engine failed to render Notion, modern SPAs, and other sites where the DOM assembles client-side.

The browser is a commodity dependency, not a Kernos problem to solve. We use Chromium via Playwright; the MCP server is just the adapter that exposes Kernos's seven agent-facing tools on top of it.

## Why Playwright (not crawl4ai, not playwright-mcp)

The original spec leaned toward `crawl4ai`. During implementation research, two concerns surfaced:

- `crawl4ai`'s MCP surface only exposes 2 of our 7 tools natively. The other 5 would still need a hand-rolled wrapper.
- `crawl4ai`-as-library has no accessibility-tree primitive, so `semantic_tree` and `interactiveElements` would be reconstructed from raw HTML — a worse approximation of what Lightpanda offered callers.

`microsoft/playwright-mcp` was a stronger semantic match (accessibility-tree = `semantic_tree` + `interactiveElements`), but mounting a Node-based MCP for a core primitive and remapping its tool names to ours cost more than the drop-in alternative.

**Chosen:** Playwright directly, driven by a ~300-LOC Python MCP server in-tree. 1:1 tool-shape match with what Lightpanda gave callers, single runtime (Python + a Chromium binary), exact control over output schemas.

## Runtime shape

One Chromium instance, one `BrowserContext`, one persistent `Page` per MCP session. Tools accept an optional `url` param for the common "navigate and extract in one call" idiom; if `url` is omitted, the tool operates on the page loaded by the last `goto`. This matches Lightpanda's semantics so existing agent code ports without churn.

| Tool | Playwright primitive | Notes |
|---|---|---|
| `goto` | `page.goto` | `wait_until="domcontentloaded"` plus a best-effort `networkidle` tail |
| `markdown` | `page.content()` → `markdownify` | ATX heading style |
| `links` | `page.content()` → BeautifulSoup parse | Deduped `{text, href}`; javascript: hrefs dropped |
| `semantic_tree` | CDP `Accessibility.getFullAXTree` | Flat node list reassembled into a nested tree, then simplified by collapsing generic wrappers and dropping Ignored nodes |
| `interactiveElements` | CDP `Accessibility.getFullAXTree` | Same tree, flattened to role-based interactive subset (`RootWebArea`/`WebArea`/`document` explicitly excluded from the focusable fallback) |
| `structuredData` | `page.content()` → BeautifulSoup parse | JSON-LD + OpenGraph + Twitter + meta/title/canonical |
| `evaluate` | `page.evaluate` | Surface errors as `RuntimeError` |

Extraction helpers that don't need the browser live in `kernos/browser/extractors.py` — testable without spawning Chromium.

**Accessibility via CDP.** Playwright ≥1.54 removed the `page.accessibility` shortcut. The canonical replacement is a Chrome DevTools Protocol session obtained via `context.new_cdp_session(page)` — Kernos opens one CDP session per active page, calls `Accessibility.getFullAXTree`, and reassembles the flat node list into the nested tree shape the extractor consumes. The constraint: every `BrowserSession.ensure_page()` must attach a CDP session, otherwise `semantic_tree` and `interactiveElements` raise. A regression guard (`tests/test_browser_extractors.py::test_cdp_ax_nodes_to_tree_*`) plus a live-test shape assertion (`BROWSER-ACCESSIBILITY-REPAIR-live-test.md`) protect the path.

**A11y quality varies by site.** Sites with thin ARIA annotation produce thin semantic trees. The tree is faithful to whatever the page exposes — if a site labels everything `<div>` with no ARIA roles, the accessibility snapshot degrades to mostly `generic` wrappers. No fix available at this layer; callers should prefer `markdown` for content-heavy sites and reach for `semantic_tree` when page structure is needed.

## Failure modes

**Navigation timeout.** Default 30s ceiling, capped at 120s. Hits raise `RuntimeError("navigation timeout after Nms loading …")`. Per-call `timeout_ms` param overrides the default.

**Auth-gated pages.** A page that redirects to a login path (`/login`, `/signin`, `/oauth`, etc.) or returns HTTP 401 / 403 surfaces as `RuntimeError("authentication required: …")`. The adapter does not attempt to solve auth; that's a separate follow-on concern. The user gets a clear error instead of a crash or silent bogus content.

**JS evaluation error.** A script error comes back as `RuntimeError("evaluate failed: …")` with the underlying message.

**Slow tail on JS-heavy sites.** `networkidle` waits are bounded to 8 seconds or the caller-specified timeout (whichever is lower). DOM ready is sufficient; a slow XHR tail doesn't block the call.

## Install footprint

Two Python deps (`playwright`, `markdownify`) plus one Chromium binary Playwright fetches via `playwright install chromium`. ~400 MB on disk. On Linux, `playwright install-deps chromium` (or the OS equivalent `libnss3`/`libatk-bridge2.0-0`/etc.) is required if the host doesn't already have them.

The capability registration is runtime-unconditional: `sys.executable -m kernos.browser`. If the Python process can't launch the server (missing Chromium, deps uninstalled), individual tool calls surface as MCP errors — Kernos's friendly-error plumbing handles the user-facing message.

## Known limitations (follow-on specs)

- Multi-tab / multi-page sessions — one page at a time today. Ship if an agent workflow actually needs tabs.
- Authenticated sessions — cookies / headers / login flows are out of scope for this migration. A future spec can bolt on session handling.
- Download interception — `page.goto` on a file URL presently goes through Chromium's default handler; not useful for content tools.
- Screenshots / PDFs — not part of the Lightpanda tool surface we preserved; add as separate tools if a caller needs them.
