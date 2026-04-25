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
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from markdownify import markdownify
from mcp.server.fastmcp import FastMCP
from playwright.async_api import (
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


def _resolve_profile_dir() -> Path:
    """Where the persistent browser profile lives on disk.

    Default: a browser-profile subfolder under the configured data
    directory. Override by setting KERNOS_BROWSER_PROFILE_DIR for a
    second concurrent process or a separate logged-in profile.
    """
    override = os.getenv("KERNOS_BROWSER_PROFILE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    data_dir = Path(os.getenv("KERNOS_DATA_DIR", "./data")).resolve()
    return data_dir / "browser-profile"


def _resolve_chrome_channel() -> str | None:
    """Pick the Chrome channel for the persistent context.

    Real Chrome (channel="chrome") is the default when a Chrome-family
    install is detected on the host. Real Chrome is harder to fingerprint
    as automation than bundled Chromium and is the architectural reason
    this batch exists. Operators who want the bundled Chromium for
    portability or reproducibility can set KERNOS_BROWSER_FORCE_CHROMIUM
    to a truthy value to opt out.
    """
    if os.getenv("KERNOS_BROWSER_FORCE_CHROMIUM", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return None
    # Detect real Chrome / Brave / Edge by their common Linux paths.
    # Playwright also resolves channel="chrome" via its own logic on
    # macOS and Windows; the explicit Linux path-existence check here
    # is a friendlier early signal in our most-common deploy.
    candidates = (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    for path in candidates:
        if Path(path).exists():
            return "chrome"
    # Fall back to bundled Chromium if nothing matched.
    return None


class BrowserProfileLockedError(RuntimeError):
    """Friendlier wrapper around Playwright's raw profile-lock error.

    Operators hit this when Kernos is already running another browser
    against the same profile (or a stale process didn't release the
    lock cleanly). The message names the override env var so the
    operator can resolve in one step without reading source.
    """


class BrowserSession:
    """Singleton-style holder for the Playwright browser + active page.

    One persistent context per MCP process backed by a profile directory
    on disk (default: data/browser-profile/). Cookies, login state, and
    the browser fingerprint persist across Kernos restarts so sites that
    rely on returning-user signals (Notion, Reddit logged-in views,
    GitHub authenticated pages) do not see each session as a fresh
    automated bot. A CDP session is attached to the page for the
    accessibility-tree path because Playwright removed the
    page.accessibility shortcut.
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cdp = None
        self._lock = asyncio.Lock()

    @property
    def profile_dir(self) -> Path:
        return _resolve_profile_dir()

    async def ensure_page(self, *, headless: bool = True) -> Page:
        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._context is None:
                profile_dir = _resolve_profile_dir()
                profile_dir.mkdir(parents=True, exist_ok=True)
                channel = _resolve_chrome_channel()
                logger.info(
                    "BROWSER_LAUNCH: profile=%s channel=%s headless=%s",
                    profile_dir, channel or "chromium", headless,
                )
                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        str(profile_dir),
                        channel=channel,
                        headless=headless,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                        ],
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                        ignore_default_args=["--disable-extensions"],
                    )
                except Exception as exc:
                    raise self._wrap_launch_error(exc, profile_dir) from exc
            # Persistent contexts open with a default page; reuse it if
            # available, otherwise create a fresh one.
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()
            self._cdp = await self._context.new_cdp_session(self._page)
            try:
                await self._cdp.send("Accessibility.enable")
            except Exception:
                # Best-effort — getFullAXTree also works without explicit
                # enable on current Chromium; we log and continue.
                logger.debug("CDP Accessibility.enable failed; continuing")
            return self._page

    @staticmethod
    def _wrap_launch_error(exc: Exception, profile_dir: Path) -> Exception:
        """Turn cryptic Playwright lock errors into operator-readable messages."""
        message = str(exc)
        lowered = message.lower()
        if (
            "processsingleton" in lowered
            or "singletonlock" in lowered
            or "is already in use" in lowered
            or "browser is already in use" in lowered
        ):
            return BrowserProfileLockedError(
                "Browser profile already in use by another Kernos process. "
                "Either close the other process or set KERNOS_BROWSER_PROFILE_DIR "
                f"to a different directory. Current profile: {profile_dir}"
            )
        return exc

    async def cdp(self):
        """Return the CDP session bound to the active page."""
        await self.ensure_page()
        return self._cdp

    async def close(self) -> None:
        async with self._lock:
            self._cdp = None
            self._page = None
            if self._context is not None:
                # Persistent context owns the underlying browser process.
                # Closing the context is sufficient; no separate browser
                # close needed.
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
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
