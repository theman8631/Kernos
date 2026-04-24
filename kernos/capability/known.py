"""Known capabilities catalog.

Adding a new capability to KERNOS:
1. Add a CapabilityInfo entry here with status=AVAILABLE
2. Add the MCP server registration in app.py / server.py (if a server exists)
3. The registry handles the rest — system prompt, State Store, CLI
"""

from kernos.capability.registry import CapabilityInfo, CapabilityStatus

KNOWN_CAPABILITIES: list[CapabilityInfo] = [
    CapabilityInfo(
        name="google-calendar",
        display_name="Google Calendar",
        description=(
            "Check your schedule, list events, find availability. "
            "Always use calendar tools when asked about schedule, events, "
            "or appointments — never guess from memory. "
            "Use account='normal' for all calendar operations."
        ),
        category="calendar",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Google Calendar — I'll need access to your Google account.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="google-calendar",
        universal=True,
        requires_web_interface=True,
        server_command="npx",
        server_args=["@cocal/google-calendar-mcp"],
        credentials_key="google-calendar",
        env_template={"GOOGLE_OAUTH_CREDENTIALS": "{credentials}"},
        auth_args=["@cocal/google-calendar-mcp", "auth", "normal"],
        auth_probe_tool="get-current-time",
        tool_effects={
            "get-current-time": "read",
            "list-events": "read",
            "search-events": "read",
            "get-event": "read",
            "create-event": "soft_write",
            "create-events": "soft_write",
            "update-event": "soft_write",
            "delete-event": "hard_write",
            "list-calendars": "read",
            "get-calendar": "read",
            "find-free-time": "read",
            "get-freebusy": "read",
            "get-timezone": "read",
            "list-timezones": "read",
            "get-colors": "read",
            "list-colors": "read",
            "respond-to-event": "soft_write",
            "manage-accounts": "read",
        },
        tool_hints={},  # All calendar tool names are self-explanatory
    ),
    CapabilityInfo(
        name="gmail",
        display_name="Gmail",
        description="Read, categorize, and draft email responses",
        category="email",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Gmail — I'll need access to your Google account.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="gmail",
        requires_web_interface=True,
        server_command="",
        server_args=[],
        credentials_key="gmail",
        env_template={},
        tool_effects={
            "list-messages": "read",
            "get-message": "read",
            "search-messages": "read",
            "list-labels": "read",
            "get-label": "read",
            "get-thread": "read",
            "list-threads": "read",
            "create-draft": "soft_write",
            "update-draft": "soft_write",
            "list-drafts": "read",
            "get-draft": "read",
            "send-email": "hard_write",
            "send-draft": "hard_write",
            "delete-message": "hard_write",
            "trash-message": "hard_write",
            "modify-message": "soft_write",
        },
    ),
    CapabilityInfo(
        name="web-search",
        display_name="Web Search",
        description=(
            "Search the web for current information. Returns structured results "
            "(title, URL, snippet) for any query. Use this when the user asks "
            "you to search for something, find current prices, news, or facts. "
            "For reading a full page in depth, pair with the web browser."
        ),
        category="search",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can add web search — want me to set that up?",
        setup_requires=["BRAVE_API_KEY"],
        server_name="brave-search",
        tool_effects={
            "brave_web_search": "read",
            "brave_local_search": "read",
        },
        tool_hints={
            "brave_web_search": "web search",
            "brave_local_search": "nearby places",
        },
        universal=True,
    ),
    # In-tree Playwright-backed MCP server (kernos/browser/).
    # Replaces Lightpanda (weak JS execution). Requires a chromium install
    # alongside the playwright Python package — see docs/architecture/browser.md.
    CapabilityInfo(
        name="web-browser",
        display_name="Web Browser",
        description=(
            "Browse the web — navigate to a URL and read its contents. "
            "Use this to fetch full page content, follow links, or read a specific site. "
            "For finding information across the web, pair with web-search: "
            "search to find the right page, then browser to read it in depth. "
            "If web-search is unavailable, use goto on a search engine directly."
        ),
        category="search",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can browse the web for you — no setup needed.",
        setup_requires=[],
        server_name="web-browser",
        tool_effects={
            "goto": "read",
            "markdown": "read",
            "semantic_tree": "read",
            "interactiveElements": "read",
            "structuredData": "read",
            "links": "read",
            "evaluate": "soft_write",  # JS execution — gate it
        },
        tool_hints={
            "goto": "load URL",
            "markdown": "page as text",
            "evaluate": "run JS",
            "semantic_tree": "page DOM structure",
            "interactiveElements": "buttons/forms/inputs",
            "structuredData": "JSON-LD/meta tags",
            "links": "list page links",
        },
        universal=True,  # Available in all context spaces
    ),
]
