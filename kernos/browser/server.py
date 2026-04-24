"""Playwright-backed MCP browser server (stdio transport).

Replaces Lightpanda. Exposes seven tools with the same call signatures
Lightpanda used, backed by a single persistent Chromium + Page.

Tool surface:
  - goto(url, timeout_ms?)                        → status + final URL
  - markdown(url?, timeout_ms?)                   → page markdown
  - links(url?, timeout_ms?)                      → [{text, href}, ...]
  - semantic_tree(url?, timeout_ms?)              → simplified accessibility tree
  - interactiveElements(url?, timeout_ms?)        → flat interactive-node list
  - structuredData(url?, timeout_ms?)             → JSON-LD / OG / meta
  - evaluate(script, url?, timeout_ms?)           → JS return value

Each tool can accept an optional `url` param for one-shot goto+extract,
matching the shape callers were already using with Lightpanda's MCP.

Failure modes handled explicitly:
  - Navigation timeout — clear TimeoutError with the URL and ceiling
  - Auth-gated page — detect login redirects and return a clear error
  - JS evaluation error — surface the script error message
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlparse

from markdownify import markdownify
from mcp.server.fastmcp import FastMCP
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from kernos.browser.extractors import (
    cdp_ax_nodes_to_tree,
    collect_interactive_elements,
    extract_links,
    extract_structured_data,
    simplify_accessibility_tree,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000

_AUTH_PATH_MARKERS = re.compile(
    r"(?:^|/)(login|signin|sign-in|auth|authenticate|authentication|account/login|oauth|sso)(?:/|$|\?)",
    re.IGNORECASE,
)


class BrowserSession:
    """Singleton-style holder for the Playwright browser + active page.

    One Chromium instance per MCP process; one page, reused across calls,
    so that goto semantics match what Lightpanda gave callers. A CDP
    session is attached to the page so we can query the accessibility
    tree via DevTools Protocol — Playwright 1.58 removed the
    `page.accessibility` shortcut, so CDP is now the canonical path.
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cdp = None
        self._lock = asyncio.Lock()

    async def ensure_page(self) -> Page:
        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
            if self._context is None:
                self._context = await self._browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
            self._page = await self._context.new_page()
            self._cdp = await self._context.new_cdp_session(self._page)
            try:
                await self._cdp.send("Accessibility.enable")
            except Exception:
                # Best-effort — getFullAXTree also works without explicit
                # enable on current Chromium; we log and continue.
                logger.debug("CDP Accessibility.enable failed; continuing")
            return self._page

    async def cdp(self):
        """Return the CDP session bound to the active page."""
        await self.ensure_page()
        return self._cdp

    async def close(self) -> None:
        async with self._lock:
            self._cdp = None
            if self._page and not self._page.is_closed():
                try:
                    await self._page.close()
                except Exception:
                    pass
            self._page = None
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None


_session = BrowserSession()


def _clamp_timeout(requested: Optional[int]) -> int:
    if requested is None:
        return DEFAULT_TIMEOUT_MS
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    if value <= 0:
        return DEFAULT_TIMEOUT_MS
    return min(value, MAX_TIMEOUT_MS)


def _looks_auth_gated(final_url: str, status: int | None) -> bool:
    if status in {401, 403}:
        return True
    parsed = urlparse(final_url)
    if _AUTH_PATH_MARKERS.search(parsed.path or ""):
        return True
    return False


@asynccontextmanager
async def _prepare_page(url: Optional[str], timeout_ms: Optional[int]):
    """Return a loaded page; if url is supplied, navigate first.

    Raises RuntimeError with a friendly message on timeout or auth-gate.
    """
    page = await _session.ensure_page()
    timeout = _clamp_timeout(timeout_ms)
    page.set_default_timeout(timeout)
    if url:
        try:
            response = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"navigation timeout after {timeout}ms loading {url}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"navigation failed for {url}: {exc}") from exc

        status = response.status if response else None
        final_url = page.url
        if _looks_auth_gated(final_url, status):
            raise RuntimeError(
                "authentication required: "
                f"{url} redirected to {final_url} "
                f"(status {status}). Auth-gated sites are out of scope."
            )

        # Best-effort idle wait so JS-heavy sites finish rendering —
        # timeout-safe. A slow tail doesn't fail the call if DOM is ready.
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout, 8_000))
        except PlaywrightTimeoutError:
            logger.debug("networkidle wait timed out for %s; continuing", url)
    yield page


mcp = FastMCP("kernos-browser")


@mcp.tool(description="Navigate the browser to the given URL and wait for DOM load.")
async def goto(url: str, timeout_ms: int | None = None) -> dict[str, Any]:
    async with _prepare_page(url, timeout_ms) as page:
        return {
            "url": page.url,
            "title": await page.title(),
        }


@mcp.tool(
    description=(
        "Get the current page content as markdown. Pass `url` to navigate "
        "and read in one step."
    )
)
async def markdown(url: str | None = None, timeout_ms: int | None = None) -> str:
    async with _prepare_page(url, timeout_ms) as page:
        html = await page.content()
    return markdownify(html, heading_style="ATX").strip()


@mcp.tool(
    description=(
        "Extract all links from the current page. Pass `url` to navigate "
        "and extract in one step. Returns a list of {text, href}."
    )
)
async def links(url: str | None = None, timeout_ms: int | None = None) -> list[dict[str, str]]:
    async with _prepare_page(url, timeout_ms) as page:
        html = await page.content()
        base = page.url
    return extract_links(html, base_url=base)


@mcp.tool(
    description=(
        "Return the page's simplified accessibility tree. Useful for "
        "understanding page structure before interacting. Pass `url` to "
        "navigate and snapshot in one step."
    )
)
async def semantic_tree(
    url: str | None = None, timeout_ms: int | None = None
) -> dict[str, Any] | None:
    async with _prepare_page(url, timeout_ms) as page:  # noqa: F841 — ensures page loaded
        cdp = await _session.cdp()
        ax = await cdp.send("Accessibility.getFullAXTree")
    tree = cdp_ax_nodes_to_tree(ax.get("nodes") or [])
    return simplify_accessibility_tree(tree)


@mcp.tool(
    description=(
        "List interactive elements on the page: buttons, links, form inputs, "
        "tabs, switches, etc. Pass `url` to navigate and extract in one step."
    )
)
async def interactiveElements(
    url: str | None = None, timeout_ms: int | None = None
) -> list[dict[str, Any]]:
    async with _prepare_page(url, timeout_ms) as page:  # noqa: F841 — ensures page loaded
        cdp = await _session.cdp()
        ax = await cdp.send("Accessibility.getFullAXTree")
    tree = cdp_ax_nodes_to_tree(ax.get("nodes") or [])
    return collect_interactive_elements(tree)


@mcp.tool(
    description=(
        "Extract structured data: JSON-LD, OpenGraph, Twitter Cards, and "
        "essential meta tags. Pass `url` to navigate and extract in one step."
    )
)
async def structuredData(
    url: str | None = None, timeout_ms: int | None = None
) -> dict[str, Any]:
    async with _prepare_page(url, timeout_ms) as page:
        html = await page.content()
    return extract_structured_data(html)


@mcp.tool(
    description=(
        "Run arbitrary JavaScript in the page context. The script runs as an "
        "IIFE; the return value is serialized back. Pass `url` to navigate "
        "first."
    )
)
async def evaluate(
    script: str, url: str | None = None, timeout_ms: int | None = None
) -> Any:
    async with _prepare_page(url, timeout_ms) as page:
        try:
            return await page.evaluate(script)
        except Exception as exc:
            raise RuntimeError(f"evaluate failed: {exc}") from exc


def run_stdio() -> None:
    """Launch the MCP server on stdio. Invoked by __main__."""
    logging.basicConfig(
        level=os.getenv("KERNOS_BROWSER_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run(transport="stdio")
