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
    ),
]
