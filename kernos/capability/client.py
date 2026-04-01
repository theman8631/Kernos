from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if TYPE_CHECKING:
    from kernos.kernel.events import EventStream

logger = logging.getLogger(__name__)

# Per-tool timeout defaults
TOOL_TIMEOUT_SECONDS: float = 30  # Default for most tools
TOOL_TIMEOUT_OVERRIDES: dict[str, float] = {
    "goto": 45,              # Browser navigation can be slow
    "markdown": 45,          # Page rendering can be slow
    "brave_web_search": 15,  # Search should be fast
    "get-current-time": 5,   # Should be instant
}

# Retry policy for transient failures
MAX_RETRIES = 1          # One retry only
RETRY_BACKOFF_S = 1.5    # Wait 1.5 seconds before retry


class MCPClientManager:
    """Manages connections to MCP servers and exposes their tools.

    Lifecycle: create once at app startup, call connect_all(), use for the
    lifetime of the process, call disconnect_all() on shutdown.
    """

    def __init__(self, events: EventStream | None = None) -> None:
        self._servers: dict[str, StdioServerParameters] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._tool_to_session: dict[str, str] = {}  # tool_name → server_name
        self._tools: list[dict] = []
        self._exit_stack = AsyncExitStack()
        self._events = events
        self._runtime_stacks: dict[str, AsyncExitStack] = {}

    def register_server(self, name: str, params: StdioServerParameters) -> None:
        """Register an MCP server configuration. Does not connect."""
        self._servers[name] = params
        logger.info("Registered MCP server: %s", name)

    async def connect_all(self) -> None:
        """Connect to all registered servers and discover their tools."""
        from kernos.kernel.event_types import EventType
        from kernos.kernel.events import emit_event

        for name, params in self._servers.items():
            try:
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._sessions[name] = session

                result = await session.list_tools()
                tool_names = []
                for tool in result.tools:
                    self._tool_to_session[tool.name] = name
                    self._tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                    )
                    tool_names.append(tool.name)
                    logger.info("Discovered tool: %s (server: %s)", tool.name, name)

                logger.info(
                    "Connected to MCP server %s — %d tools discovered",
                    name,
                    len(result.tools),
                )
                if self._events:
                    try:
                        await emit_event(
                            self._events,
                            EventType.CAPABILITY_CONNECTED,
                            "system",
                            "capability_manager",
                            payload={
                                "server_name": name,
                                "tool_count": len(tool_names),
                                "tool_names": tool_names,
                                "error": None,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to emit capability.connected: %s", exc)

            except Exception as exc:
                logger.error(
                    "Failed to connect to MCP server %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                if self._events:
                    try:
                        from kernos.kernel.event_types import EventType
                        from kernos.kernel.events import emit_event

                        await emit_event(
                            self._events,
                            EventType.CAPABILITY_ERROR,
                            "system",
                            "capability_manager",
                            payload={
                                "server_name": name,
                                "tool_count": 0,
                                "tool_names": [],
                                "error": str(exc),
                            },
                        )
                    except Exception as emit_exc:
                        logger.warning("Failed to emit capability.error: %s", emit_exc)

    async def connect_one(self, server_name: str) -> bool:
        """Connect a single MCP server at runtime. Returns True on success."""
        if server_name not in self._servers:
            return False
        if server_name in self._sessions:
            return True  # Already connected

        params = self._servers[server_name]
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server_name] = session
            self._runtime_stacks[server_name] = stack

            result = await session.list_tools()
            tool_names = []
            for tool in result.tools:
                self._tool_to_session[tool.name] = server_name
                self._tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema,
                    }
                )
                tool_names.append(tool.name)
                logger.info("Discovered tool: %s (server: %s)", tool.name, server_name)

            logger.info(
                "Connected to MCP server %s — %d tools discovered",
                server_name,
                len(result.tools),
            )
            from kernos.kernel.event_types import EventType
            from kernos.kernel.events import emit_event
            if self._events:
                try:
                    await emit_event(
                        self._events,
                        EventType.CAPABILITY_CONNECTED,
                        "system",
                        "capability_manager",
                        payload={
                            "server_name": server_name,
                            "tool_count": len(tool_names),
                            "tool_names": tool_names,
                            "error": None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit capability.connected: %s", exc)
            return True
        except Exception as exc:
            logger.warning("Failed to connect %s: %s", server_name, exc)
            try:
                await stack.aclose()
            except Exception:
                pass
            return False

    async def disconnect_one(self, server_name: str) -> bool:
        """Disconnect a single MCP server at runtime. Returns True if it was connected."""
        if server_name not in self._sessions:
            return False

        # Remove tools for this server
        self._tools = [
            t for t in self._tools
            if self._tool_to_session.get(t["name"]) != server_name
        ]
        self._tool_to_session = {
            k: v for k, v in self._tool_to_session.items() if v != server_name
        }
        del self._sessions[server_name]

        # Close runtime stack if one exists (servers connected via connect_one)
        if server_name in self._runtime_stacks:
            try:
                await self._runtime_stacks[server_name].aclose()
            except Exception:
                pass
            del self._runtime_stacks[server_name]

        if self._events:
            try:
                from kernos.kernel.event_types import EventType
                from kernos.kernel.events import emit_event
                await emit_event(
                    self._events,
                    EventType.CAPABILITY_DISCONNECTED,
                    "system",
                    "capability_manager",
                    payload={
                        "server_name": server_name,
                        "tool_count": 0,
                        "tool_names": [],
                        "error": None,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit capability.disconnected: %s", exc)

        return True

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers and clean up subprocesses."""
        if self._events:
            from kernos.kernel.event_types import EventType
            from kernos.kernel.events import emit_event

            for name in list(self._sessions.keys()):
                tools = [t for t, s in self._tool_to_session.items() if s == name]
                try:
                    await emit_event(
                        self._events,
                        EventType.CAPABILITY_DISCONNECTED,
                        "system",
                        "capability_manager",
                        payload={
                            "server_name": name,
                            "tool_count": len(tools),
                            "tool_names": tools,
                            "error": None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit capability.disconnected: %s", exc)

        for name, stack in list(self._runtime_stacks.items()):
            try:
                await stack.aclose()
            except Exception:
                pass
        self._runtime_stacks.clear()

        await self._exit_stack.aclose()
        logger.info("Disconnected from all MCP servers")

    def get_tools(self) -> list[dict]:
        """Return all available tools in Anthropic API format."""
        return list(self._tools)

    def get_tool_definitions(self) -> dict[str, list[dict]]:
        """Return tool definitions grouped by server name.

        Returns: {"google-calendar": [{"name": "get-events", ...}, ...]}
        """
        result: dict[str, list[dict]] = {}
        for tool in self._tools:
            server = self._tool_to_session.get(tool["name"], "unknown")
            result.setdefault(server, []).append(tool)
        return result

    @staticmethod
    def _is_transient(error_msg: str, exc: Exception | None = None) -> bool:
        """Distinguish transport failure from valid tool error.

        Transport failures are retryable. Application errors are not.
        """
        if isinstance(exc, asyncio.TimeoutError):
            return True
        if isinstance(exc, (ConnectionError, ConnectionResetError,
                            ConnectionRefusedError)):
            return True

        lower = error_msg.lower()

        # Explicitly NOT transient
        if any(s in lower for s in [
            "not found", "not available", "validation",
            "permission", "unauthorized", "forbidden",
            "invalid", "auth",
        ]):
            return False

        # Transient indicators
        return any(s in lower for s in [
            "503", "429", "rate limit", "timeout",
            "temporarily unavailable", "connection reset",
            "connection refused", "service unavailable",
        ])

    def _get_timeout(self, tool_name: str) -> float:
        """Return the timeout for a given tool."""
        return TOOL_TIMEOUT_OVERRIDES.get(tool_name, TOOL_TIMEOUT_SECONDS)

    async def call_tool(self, tool_name: str, tool_args: dict,
                        timeout: float | None = None) -> str:
        """Call an MCP tool by name. Returns result text or an error string.

        Never raises — all errors are returned as descriptive strings.
        Retries once on transient transport failures with 1.5s backoff.
        """
        server_name = self._tool_to_session.get(tool_name)
        if server_name is None:
            logger.error("Tool not found: %s", tool_name)
            return f"Tool error: '{tool_name}' is not available."

        session = self._sessions.get(server_name)
        if session is None:
            logger.error("Session not found for server: %s", server_name)
            return f"Tool error: server '{server_name}' is not connected."

        effective_timeout = timeout or self._get_timeout(tool_name)

        for attempt in range(1 + MAX_RETRIES):
            exc_ref: Exception | None = None
            error_msg = ""
            try:
                # Timeout wraps ONLY the actual MCP session call
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, tool_args),
                    timeout=effective_timeout,
                )
                # Formatting/processing happens OUTSIDE timeout
                parts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        parts.append(content.text)
                    else:
                        parts.append(str(content))
                return "\n".join(parts) if parts else "(empty result)"

            except asyncio.CancelledError:
                raise  # Never swallow cancellation

            except asyncio.TimeoutError as exc:
                error_msg = f"Tool error: '{tool_name}' timed out after {effective_timeout}s."
                exc_ref = exc
                logger.warning(
                    "TOOL_TIMEOUT: tool=%s timeout=%.1fs",
                    tool_name, effective_timeout,
                )

            except Exception as exc:
                error_msg = f"Tool error: {exc}"
                exc_ref = exc

            # Check if retryable
            if attempt < MAX_RETRIES and self._is_transient(error_msg, exc_ref):
                logger.info(
                    "TOOL_RETRY: tool=%s attempt=%d/%d backoff=%.1fs error=%s",
                    tool_name, attempt + 1, MAX_RETRIES + 1,
                    RETRY_BACKOFF_S, error_msg[:100],
                )
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue

            # Not retryable or retries exhausted
            logger.warning(
                "TOOL_FAILED: tool=%s attempts=%d error=%s",
                tool_name, attempt + 1, error_msg[:200],
            )
            return error_msg

        # Should not reach here, but satisfy type checker
        return error_msg  # type: ignore[possibly-undefined]
