"""Entry point for `python -m kernos.browser`.

Two subcommands:
- `serve` (default) — runs the stdio MCP server. This is what
  kernos/server.py and kernos/chat.py invoke when registering the
  in-process browser capability.
- `login [url]` — opens a headful persistent context against the
  configured profile so the operator can authenticate manually into a
  site (Notion, GitHub, Reddit, etc.). Cookies and session state
  persist into the next regular Kernos run automatically.
- `info` — prints the resolved profile path and channel selection.
  Useful for confirming where the persistent profile lives before
  logging into anything.

Surface this in the standard help path: `python -m kernos.browser --help`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from kernos.browser.server import (
    BrowserProfileLockedError,
    _resolve_chrome_channel,
    _resolve_profile_dir,
    run_stdio,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kernos.browser",
        description=(
            "Kernos browser MCP server and helpers. "
            "Default subcommand is `serve` (stdio MCP server). "
            "Use `login` once per site to authenticate the persistent "
            "profile, then regular Kernos browsing reuses the session."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "serve",
        help="Run the stdio MCP server (default; what Kernos's server invokes).",
    )

    login = sub.add_parser(
        "login",
        help=(
            "Open a headful browser against the persistent profile so you "
            "can log into a site. Cookies persist for future Kernos runs."
        ),
    )
    login.add_argument(
        "--url",
        default="about:blank",
        help=(
            "URL to land on. Pass the login page directly "
            "(for example, the Notion login page) so you don't have to "
            "navigate after the window opens."
        ),
    )
    login.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help=(
            "How long to wait for the operator to close the window before "
            "the subcommand exits. Defaults to ten minutes."
        ),
    )

    sub.add_parser(
        "info",
        help="Print the resolved profile directory and Chrome-channel choice.",
    )

    return parser


async def _login(url: str, timeout_seconds: int) -> int:
    from playwright.async_api import async_playwright

    profile_dir = _resolve_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    channel = _resolve_chrome_channel()
    print(f"opening headful browser: profile={profile_dir} channel={channel or 'chromium'}")
    print(f"landing on: {url}")
    print(
        "log in normally, then close the window (or wait "
        f"{timeout_seconds}s) — cookies will persist for next Kernos run."
    )
    pw = await async_playwright().start()
    try:
        try:
            ctx = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                channel=channel,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                viewport={"width": 1280, "height": 900},
                ignore_default_args=["--disable-extensions"],
            )
        except Exception as exc:
            msg = str(exc).lower()
            if (
                "processsingleton" in msg
                or "singletonlock" in msg
                or "is already in use" in msg
            ):
                print(
                    "ERROR: Browser profile already in use by another "
                    "Kernos process. Either stop the other process or "
                    "set KERNOS_BROWSER_PROFILE_DIR to a different "
                    f"directory. Current profile: {profile_dir}",
                    file=sys.stderr,
                )
                return 2
            raise
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        # Wait for the operator to close the window. Polling is cheap.
        elapsed = 0.0
        step = 0.5
        while elapsed < timeout_seconds:
            try:
                if not ctx.pages:
                    break
                await ctx.pages[0].evaluate("1")
            except Exception:
                # Page closed or context torn down.
                break
            await asyncio.sleep(step)
            elapsed += step
        try:
            await ctx.close()
        except Exception:
            pass
    finally:
        await pw.stop()
    print("login flow complete; cookies persisted.")
    return 0


async def _info() -> int:
    profile_dir = _resolve_profile_dir()
    channel = _resolve_chrome_channel()
    profile_exists = profile_dir.exists()
    print(f"profile directory: {profile_dir}")
    print(f"profile exists:    {profile_exists}")
    print(f"chrome channel:    {channel or 'chromium (bundled fallback)'}")
    print()
    print("override env vars:")
    print("  KERNOS_BROWSER_PROFILE_DIR  — point at a different profile")
    print("  KERNOS_BROWSER_FORCE_CHROMIUM=1  — opt out of real Chrome")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or "serve"
    if cmd == "serve":
        run_stdio()
        return 0
    if cmd == "login":
        return asyncio.run(_login(args.url, args.timeout_seconds))
    if cmd == "info":
        return asyncio.run(_info())
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
