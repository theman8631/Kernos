"""Known capabilities catalog.

Adding a new capability to KERNOS:
1. Add a CapabilityInfo entry here with status=AVAILABLE
2. Add the MCP server registration in app.py / discord_bot.py (if a server exists)
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
            "or appointments — never guess from memory."
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
        tool_effects={
            "get-current-time": "read",
            "list-events": "read",
            "search-events": "read",
            "get-event": "read",
            "create-event": "soft_write",
            "update-event": "soft_write",
            "delete-event": "hard_write",
            "list-calendars": "read",
            "get-calendar": "read",
            "find-free-time": "read",
            "get-timezone": "read",
            "list-timezones": "read",
            "get-colors": "read",
        },
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
        description="Search the internet for current information",
        category="search",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can add web search — want me to set that up?",
        setup_requires=[],
        server_name="",
        tool_effects={
            "search": "read",
            "fetch": "read",
        },
    ),
]
