"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event, estimate_cost
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)

logger = logging.getLogger(__name__)

_PROVIDER = "anthropic"
_SIMPLE_MODEL = "claude-sonnet-4-6"  # Used by complete_simple()
_CHEAP_MODEL = "claude-haiku-4-5-20251001"  # Used by complete_simple() when prefer_cheap=True

_OPENAI_SIMPLE_MODEL = "gpt-4o"      # Used by complete_simple() for OpenAI
_OPENAI_CHEAP_MODEL = "gpt-4o-mini"  # Used by complete_simple(prefer_cheap=True) for OpenAI


REQUEST_TOOL = {
    "name": "request_tool",
    "description": (
        "Request activation of a tool capability for the current context space. "
        "Use this when you need a tool that isn't currently available. "
        "Describe what you need thoroughly — what the tool should do, why you need it, "
        "and what context it's for. This helps the system find the right match."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability_name": {
                "type": "string",
                "description": (
                    "The name of the capability to activate, if known. "
                    "Use 'unknown' if you know what you need but not the exact name."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Thorough description of what you need the tool to do. "
                    "Be exhaustive — include the function needed, the context, "
                    "and why it's needed. This helps match the right tool."
                ),
            },
        },
        "required": ["capability_name", "description"],
    },
}


READ_DOC_TOOL = {
    "name": "read_doc",
    "description": (
        "Read Kernos documentation. Use when you need to understand a capability, "
        "behavior, or how the system works. Your docs are at docs/ — read the "
        "relevant section to answer accurately. "
        "Examples: 'index.md', 'capabilities/web-browsing.md', 'behaviors/covenants.md', "
        "'architecture/memory.md', 'identity/who-you-are.md', 'roadmap/vision.md'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to docs/. "
                    "Examples: 'index.md', 'capabilities/web-browsing.md', "
                    "'behaviors/covenants.md', 'architecture/context-spaces.md'"
                ),
            },
        },
        "required": ["path"],
    },
}


REMEMBER_DETAILS_TOOL = {
    "name": "remember_details",
    "description": (
        "Retrieve exact conversation text from a specific archived source log. "
        "Use after remember() when a Ledger entry includes 'source: log_NNN'. "
        "Optional query narrows to the relevant section within that log. "
        "This is a read-only operation — no state is changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_ref": {
                "type": "string",
                "description": (
                    "The log reference to retrieve, e.g., 'log_003'. "
                    "Get this from a Ledger entry returned by remember()."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional keyword to find the relevant section within "
                    "the log. Returns matching lines with surrounding context. "
                    "If omitted, returns the full log (bounded)."
                ),
            },
        },
        "required": ["source_ref"],
    },
}


def _read_doc(path: str) -> str:
    """Read a Kernos documentation file from docs/.

    Security: only allows reads within the docs/ directory.
    Rejects paths with '..', absolute paths, or paths outside docs/.
    """
    from pathlib import Path

    if path.startswith("/") or path.startswith("\\"):
        return "Error: Absolute paths are not allowed. Use a relative path like 'capabilities/web-browsing.md'."

    if ".." in path:
        return "Error: Path traversal ('..') is not allowed."

    # Resolve docs/ root relative to the repo
    import importlib
    kernos_root = Path(importlib.import_module("kernos").__file__).parent
    docs_root = kernos_root.parent / "docs"
    target = (docs_root / path).resolve()

    if not str(target).startswith(str(docs_root.resolve())):
        return "Error: Path resolves outside the docs/ directory."

    if not target.exists():
        # List available files to help the agent find the right one
        available = []
        for f in sorted(docs_root.rglob("*.md")):
            available.append(str(f.relative_to(docs_root)))
        hint = "\n".join(f"  - {a}" for a in available[:30])
        return f"Error: File not found: docs/{path}\n\nAvailable docs:\n{hint}"

    if not target.is_file():
        return f"Error: Not a file: docs/{path}"

    return target.read_text(encoding="utf-8")


MANAGE_CAPABILITIES_TOOL = {
    "name": "manage_capabilities",
    "description": (
        "Manage connected services — list, enable, disable, install, or remove capabilities. "
        "Use 'list' to see all services and their connection status. "
        "Use 'enable' or 'disable' to toggle a service on or off. "
        "Use 'install' to add a new MCP server. "
        "Use 'remove' to uninstall a user-added capability "
        "(defaults can only be disabled, not removed). "
        "Note: to see which tools are available, check the TOOLS section in your instructions — "
        "this command manages services, not individual tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "enable", "disable", "install", "remove"],
                "description": "The action to perform.",
            },
            "capability": {
                "type": "string",
                "description": "The capability name (required for enable/disable/remove).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


READ_SOURCE_TOOL = {
    "name": "read_source",
    "description": (
        "Read Kernos source code. Use when the user asks how something works "
        "technically or wants to see implementation details. Only reads files "
        "within the kernos/ package directory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path within the kernos/ package. "
                    "Examples: 'kernel/awareness.py', 'kernel/reasoning.py', "
                    "'messages/handler.py', 'capability/registry.py'"
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional class or function name to extract. "
                    "Examples: 'AwarenessEvaluator', 'run_time_pass', '_gate_tool_call'. "
                    "If omitted, returns the full file."
                ),
            },
        },
        "required": ["path"],
    },
}


READ_SOUL_TOOL = {
    "name": "read_soul",
    "description": (
        "Read your own identity — who you are, your personality, your relationship "
        "with this user. Use this when you want to understand or verify your own state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


UPDATE_SOUL_TOOL = {
    "name": "update_soul",
    "description": (
        "Update your own identity — name, emoji, personality notes, communication "
        "style. Use when the user asks you to change something about yourself, or "
        "when you and the user agree on a change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": (
                    "The soul field to update. Allowed: agent_name, emoji, "
                    "personality_notes, communication_style."
                ),
            },
            "value": {
                "type": "string",
                "description": "The new value for the field.",
            },
        },
        "required": ["field", "value"],
    },
}


# Allowed fields for update_soul — lifecycle and user fields are read-only
_SOUL_UPDATABLE_FIELDS = {"agent_name", "emoji", "personality_notes", "communication_style"}


def _read_source(path: str, section: str = "") -> str:
    """Read Kernos source code. Returns file contents or extracted section.

    Security: only allows reads within the kernos/ package directory.
    Rejects paths with '..', absolute paths, or paths outside kernos/.
    """
    import importlib
    from pathlib import Path

    # Security: reject absolute paths
    if path.startswith("/") or path.startswith("\\"):
        return "Error: Absolute paths are not allowed. Use a relative path like 'kernel/awareness.py'."

    # Security: reject path traversal
    if ".." in path:
        return "Error: Path traversal ('..') is not allowed."

    # Resolve kernos package root
    kernos_root = Path(importlib.import_module("kernos").__file__).parent
    target = (kernos_root / path).resolve()

    # Security: ensure resolved path is within kernos/
    if not str(target).startswith(str(kernos_root)):
        return "Error: Path resolves outside the kernos/ package directory."

    if not target.exists():
        return f"Error: File not found: kernos/{path}"

    if not target.is_file():
        return f"Error: Not a file: kernos/{path}"

    if target.suffix not in (".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml"):
        return f"Error: Unsupported file type: {target.suffix}"

    content = target.read_text(encoding="utf-8")

    if not section:
        # Cap at 500 lines for full files
        lines = content.split("\n")
        if len(lines) > 500:
            return "\n".join(lines[:500]) + f"\n\n... (truncated — {len(lines)} total lines)"
        return content

    # Extract a class or function section
    lines = content.split("\n")
    start_idx = None
    start_indent = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"class {section}") or stripped.startswith(f"def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break
        # Also match async def
        if stripped.startswith(f"async def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break

    if start_idx is None:
        return f"Error: Section '{section}' not found in kernos/{path}"

    # Find end: next definition at same or lower indent level
    result_lines = [lines[start_idx]]
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            result_lines.append(line)
            continue
        current_indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        # Same-level or lower-level class/def = end of section
        if current_indent <= start_indent and (
            stripped.startswith("class ")
            or stripped.startswith("def ")
            or stripped.startswith("async def ")
            or stripped.startswith("# ---")
        ):
            break
        result_lines.append(line)

    # Strip trailing blank lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    return "\n".join(result_lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# KERNOS-native content types — no provider types leak past this module
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    """A single content block from a provider response. Provider-agnostic."""

    type: str
    text: str | None = None
    name: str | None = None
    id: str | None = None
    input: dict | None = None


@dataclass
class ProviderResponse:
    """Provider response in KERNOS-native format."""

    content: list[ContentBlock]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class Provider(ABC):
    """Abstract LLM provider. Each implementation wraps a specific SDK."""

    @abstractmethod
    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
    ) -> ProviderResponse:
        """Send a completion request and return a KERNOS-native response."""
        ...


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------


class AnthropicProvider(Provider):
    """Wraps the Anthropic SDK. Maps SDK exceptions to KERNOS exceptions."""

    provider_name = "anthropic"
    main_model = _SIMPLE_MODEL
    simple_model = _SIMPLE_MODEL
    cheap_model = _CHEAP_MODEL

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
    ) -> ProviderResponse:
        # Apply prompt caching: cache_control on system prompt and last tool
        cached_system = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        cached_tools = list(tools) if tools else []
        if cached_tools:
            cached_tools[-1] = {
                **cached_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": cached_system,
            "messages": messages,
        }
        if cached_tools:
            create_kwargs["tools"] = cached_tools
        if output_schema:
            create_kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }

        try:
            response = await self._client.messages.create(**create_kwargs)
        except anthropic.APITimeoutError as exc:
            raise ReasoningTimeoutError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ReasoningConnectionError(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ReasoningRateLimitError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ReasoningProviderError(f"API status {exc.status_code}: {exc}") from exc
        except Exception as exc:
            raise ReasoningProviderError(str(exc)) from exc

        content = [
            ContentBlock(
                type=block.type,
                text=getattr(block, "text", None),
                name=getattr(block, "name", None),
                id=getattr(block, "id", None),
                input=getattr(block, "input", None),
            )
            for block in response.content
        ]

        cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

        return ProviderResponse(
            content=content,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
        )


# ---------------------------------------------------------------------------
# OpenAI Codex implementation — uses ChatGPT OAuth, not standard API key
# ---------------------------------------------------------------------------


class OpenAICodexProvider(Provider):
    """ChatGPT Codex OAuth provider — uses chatgpt.com/backend-api/codex/responses.

    NOT the standard OpenAI API. Mirrors OpenClaw's openai-codex-responses transport:
    - Endpoint: https://chatgpt.com/backend-api/codex/responses
    - Body: OpenAI Responses API format (instructions, input, tools)
    - Headers: Bearer token, chatgpt-account-id, originator: pi, OpenAI-Beta
    - Auth: ChatGPT OAuth credentials, not OPENAI_API_KEY
    """

    provider_name = "openai-codex"

    def __init__(
        self,
        credential: "OpenAICodexCredential",
        model: str = "",
    ) -> None:
        self._credential = credential
        self.main_model = model or os.getenv("OPENAI_CODEX_MODEL", "gpt-5.4")
        self.simple_model = os.getenv("OPENAI_CODEX_SIMPLE_MODEL", self.main_model)
        self.cheap_model = os.getenv("OPENAI_CODEX_CHEAP_MODEL", "gpt-5.4-nano")
        self._base_url = os.getenv(
            "OPENAI_CODEX_BASE_URL", "https://chatgpt.com/backend-api"
        )
        self._http: Any = None

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=120.0)
        return self._http

    async def _ensure_valid_token(self) -> None:
        """Refresh the access token if expired or within 60s of expiry."""
        now_ms = int(time.time() * 1000)
        if self._credential["expires"] and self._credential["expires"] > now_ms + 60_000:
            return
        from kernos.kernel.credentials import refresh_openai_codex_credential
        logger.info("CODEX_REFRESH: token expired or near expiry, refreshing")
        self._credential = await refresh_openai_codex_credential(self._credential)

    def _headers(self) -> dict[str, str]:
        """Build request headers matching OpenClaw's Codex wire contract."""
        return {
            "Authorization": f"Bearer {self._credential['access']}",
            "chatgpt-account-id": self._credential["accountId"],
            "originator": "pi",
            "User-Agent": "pi (python)",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "accept": "application/json",
        }

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool defs to OpenAI Responses API function format."""
        result = []
        for t in tools:
            result.append({
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _translate_input(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format messages to OpenAI Responses API input items.

        The Responses API uses a flat list of typed items, not chat-style messages.
        """
        items: list[dict] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Plain string content
            if isinstance(content, str):
                if role == "assistant":
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                else:
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    })
                continue

            # List content — Anthropic continuation format
            if isinstance(content, list):
                tool_calls = []
                text_parts = []
                tool_results = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        tool_calls.append(block)
                    elif btype == "tool_result":
                        tool_results.append(block)
                    elif btype == "text":
                        text_parts.append(block.get("text", ""))

                if tool_calls:
                    # Assistant message with text + function calls
                    if text_parts:
                        text = "".join(text_parts)
                        items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                    for tc in tool_calls:
                        items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("input", {})),
                        })
                elif tool_results:
                    for tr in tool_results:
                        items.append({
                            "type": "function_call_output",
                            "call_id": tr.get("tool_use_id", ""),
                            "output": tr.get("content", ""),
                        })
                elif text_parts:
                    items.append({
                        "type": "message",
                        "role": role if role != "assistant" else "user",
                        "content": [{"type": "input_text", "text": "".join(text_parts)}],
                    })

        return items

    @staticmethod
    def _parse_response(data: dict) -> ProviderResponse:
        """Parse OpenAI Responses API response into Kernos-native format."""
        status = data.get("status", "completed")
        output_items = data.get("output", [])

        # Map status to stop_reason
        if status == "incomplete":
            stop_reason = "max_tokens"
        elif any(item.get("type") == "function_call" for item in output_items):
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        content_blocks: list[ContentBlock] = []

        for item in output_items:
            item_type = item.get("type", "")

            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content_blocks.append(
                            ContentBlock(type="text", text=part.get("text", ""))
                        )

            elif item_type == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                content_blocks.append(ContentBlock(
                    type="tool_use",
                    id=item.get("call_id", item.get("id", "")),
                    name=item.get("name", ""),
                    input=args,
                ))

        if not content_blocks:
            content_blocks.append(ContentBlock(type="text", text=""))

        usage = data.get("usage", {})
        return ProviderResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
        )

    def _resolve_url(self) -> str:
        """Build the Codex responses endpoint URL."""
        base = self._base_url.rstrip("/")
        if base.endswith("/codex/responses"):
            return base
        if base.endswith("/codex"):
            return f"{base}/responses"
        return f"{base}/codex/responses"

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
    ) -> ProviderResponse:
        await self._ensure_valid_token()
        http = await self._ensure_http()

        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": self._translate_input(messages),
            "store": False,
            "stream": True,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
        if output_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": output_schema,
                }
            }

        url = self._resolve_url()
        headers = self._headers()
        headers["accept"] = "text/event-stream"

        try:
            import httpx as _httpx
            async with http.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code == 401:
                    await resp.aread()
                    raise ReasoningProviderError(
                        f"Codex auth failed (401): {resp.text[:300]}"
                    )
                if resp.status_code == 429:
                    await resp.aread()
                    raise ReasoningRateLimitError(
                        f"Codex rate limited (429): {resp.text[:300]}"
                    )
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ReasoningProviderError(
                        f"Codex API error ({resp.status_code}): {resp.text[:300]}"
                    )
                # Parse SSE stream, collect the response.completed event
                data = await self._collect_sse_response(resp)
        except (ReasoningRateLimitError, ReasoningProviderError):
            raise
        except Exception as exc:
            raise ReasoningConnectionError(f"Codex request failed: {exc}") from exc

        return self._parse_response(data)

    @staticmethod
    async def _collect_sse_response(resp: Any) -> dict:
        """Read an SSE stream and return the final response object.

        Collects response.completed/response.done event which contains
        the full response with output items and usage.
        """
        final_response: dict = {}
        buffer = ""

        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                idx = buffer.index("\n\n")
                block = buffer[:idx]
                buffer = buffer[idx + 2:]

                data_lines = [
                    line[5:].strip() for line in block.split("\n")
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                data_str = "\n".join(data_lines).strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                # Capture the final response from completion events
                if event_type in ("response.completed", "response.done"):
                    final_response = event.get("response", event)
                elif event_type == "response.failed":
                    msg = ""
                    if "response" in event:
                        err = event["response"].get("error", {})
                        msg = err.get("message", "")
                    raise ReasoningProviderError(
                        f"Codex response failed: {msg or event_type}"
                    )
                elif event_type == "error":
                    msg = event.get("message", event.get("code", "unknown"))
                    raise ReasoningProviderError(f"Codex stream error: {msg}")

        if not final_response:
            raise ReasoningProviderError("Codex stream ended without response.completed")

        return final_response


# ---------------------------------------------------------------------------
# Request / Result types
# ---------------------------------------------------------------------------


@dataclass
class ReasoningRequest:
    """Everything the ReasoningService needs to run a reasoning turn."""

    tenant_id: str
    conversation_id: str
    system_prompt: str
    messages: list[dict]
    tools: list[dict]
    model: str
    trigger: str
    max_tokens: int = 64000  # Sonnet/Opus output limit — let the model decide when to stop
    active_space_id: str = ""  # For kernel tool routing (e.g., remember)
    input_text: str = ""       # Current user message — used by dispatch gate
    active_space: Any = None   # ContextSpace | None — for gate tool effect classification


@dataclass
class GateResult:
    """The outcome of a dispatch gate check."""

    allowed: bool
    reason: str    # "explicit_instruction", "covenant_authorized", "covenant_conflict", "denied",
                   # "token_approved"
    method: str    # "token", "model_check"
    proposed_action: str = ""    # Human-readable description of what was blocked
    conflicting_rule: str = ""   # For CONFLICT — which rule conflicts
    raw_response: str = ""       # Full model response for logging


@dataclass
class ApprovalToken:
    """Single-use token issued when the dispatch gate blocks an action.

    The agent re-submits the tool call with ``_approval_token: '{token_id}'``
    in the tool input to bypass the gate after explicit user confirmation.
    """

    token_id: str          # uuid hex[:12]
    tool_name: str
    tool_input_hash: str   # md5 hex[:8] of tool_input (after popping _approval_token)
    issued_at: datetime
    used: bool = False


@dataclass
class PendingAction:
    """A tool call blocked by the dispatch gate, awaiting user confirmation.

    Stored on the ReasoningService keyed by tenant_id. The handler executes
    confirmed actions after the agent signals [CONFIRM:N] in its response.
    """

    tool_name: str
    tool_input: dict
    proposed_action: str      # Human-readable description
    conflicting_rule: str     # Populated for CONFLICT; empty for DENIED
    gate_reason: str          # "covenant_conflict" or "denied"
    expires_at: datetime      # 5 minutes from creation (UTC)


@dataclass
class ReasoningResult:
    """The outcome of a reasoning turn."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    tool_iterations: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _block_to_api_dict(block: ContentBlock) -> dict:
    """Convert a ContentBlock to an Anthropic API-compatible dict for continuation messages."""
    if block.type == "text":
        return {"type": "text", "text": block.text or ""}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id or "",
            "name": block.name or "",
            "input": block.input or {},
        }
    return {"type": block.type}


# ---------------------------------------------------------------------------
# ReasoningService
# ---------------------------------------------------------------------------


class ReasoningService:
    """Owns the full tool-use reasoning loop. Provider-agnostic.

    Emits reasoning.request, reasoning.response, tool.called, tool.result events.
    Logs tool calls and results to the audit store.
    Raises ReasoningError subtypes on provider failure — does NOT catch them.
    """

    MAX_TOOL_ITERATIONS = 10

    def __init__(
        self,
        provider: Provider,
        events: EventStream,
        mcp: Any,    # MCPClientManager — Any avoids circular import with capability layer
        audit: Any,  # AuditStore
    ) -> None:
        self._provider = provider
        self._events = events
        self._mcp = mcp
        self._audit = audit
        self._retrieval = None  # Set by handler after construction (avoids circular import)
        self._files = None      # Set by handler after construction
        self._registry = None   # Set by handler after construction
        self._state = None      # Set by handler after construction
        self._channel_registry = None  # Set by handler after construction
        self._trigger_store = None     # Set by handler after construction
        self._handler = None           # Set by handler after construction (for schedule tool)
        self._approval_tokens: dict[str, ApprovalToken] = {}  # In-memory token store
        self._pending_actions: dict[str, list[PendingAction]] = {}  # tenant_id → list
        self._tools_changed: bool = False  # Set by manage_capabilities; handler checks post-reasoning
        # Lazy tool loading: tracks which MCP tools have been loaded per-space session
        self._loaded_tools: dict[str, set[str]] = {}  # space_id → set of tool names

    def set_retrieval(self, retrieval: Any) -> None:
        """Wire up the retrieval service for kernel tool routing."""
        self._retrieval = retrieval

    def set_files(self, files: Any) -> None:
        """Wire up the file service for kernel tool routing."""
        self._files = files

    def set_registry(self, registry: Any) -> None:
        """Wire up the capability registry for request_tool routing."""
        self._registry = registry

    def set_state(self, state: Any) -> None:
        """Wire up the state store for request_tool activation."""
        self._state = state

    def get_loaded_tools(self, space_id: str) -> set[str]:
        """Get the set of MCP tool names currently loaded for a space."""
        return self._loaded_tools.get(space_id, set())

    def load_tool(self, space_id: str, tool_name: str) -> None:
        """Add a tool to the loaded set for a space."""
        if space_id not in self._loaded_tools:
            self._loaded_tools[space_id] = set()
        self._loaded_tools[space_id].add(tool_name)

    def clear_loaded_tools(self, space_id: str) -> None:
        """Clear loaded tools for a space (session boundary)."""
        count = len(self._loaded_tools.pop(space_id, set()))
        if count:
            logger.info("TOOL_UNLOAD: space=%s cleared=%d", space_id, count)

    async def complete_simple(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 1024,
        prefer_cheap: bool = False,
        output_schema: dict | None = None,
    ) -> str:
        """Single stateless completion. No tools, no history, no task events.

        Used by kernel infrastructure (extraction, consolidation) not by agents.
        Returns raw text response. prefer_cheap uses Haiku-class model for cost efficiency.

        When output_schema is provided, uses Anthropic's native structured outputs
        (constrained decoding). Schema compliance is guaranteed by the API — no
        json.loads() retry logic needed. Returns "{}" on truncation or refusal.
        """
        model = (
            getattr(self._provider, "cheap_model", _CHEAP_MODEL) if prefer_cheap
            else getattr(self._provider, "simple_model", _SIMPLE_MODEL)
        )
        response = await self._provider.complete(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=[],
            max_tokens=max_tokens,
            output_schema=output_schema,
        )
        # Log token usage on every simple completion
        logger.info(
            "SIMPLE_RESPONSE: tokens_in=%d tokens_out=%d truncated=%s",
            response.input_tokens, response.output_tokens,
            response.stop_reason == "max_tokens",
        )
        if response.stop_reason == "max_tokens":
            text_preview = "".join(b.text for b in response.content if b.type == "text")
            logger.warning(
                "complete_simple: response truncated (max_tokens=%d) preview=%s",
                max_tokens, text_preview[:200],
            )
            if output_schema:
                return "{}"
            # Plain-text call: return whatever was generated (partial is better than "{}")
        if response.stop_reason == "refusal":
            logger.warning("complete_simple: response refused by model")
            return "{}"
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "".join(text_parts)

    # Kernel tools: intercepted before MCP, never passed through to external servers
    _KERNEL_TOOLS = {"remember", "remember_details", "write_file", "read_file", "list_files", "delete_file", "dismiss_whisper", "read_source", "read_doc", "read_soul", "update_soul", "manage_covenants", "manage_capabilities", "manage_channels", "manage_schedule"}

    # ---------------------------------------------------------------------------
    # Dispatch Gate (3D-HOTFIX)
    # ---------------------------------------------------------------------------

    def _classify_tool_effect(self, tool_name: str, active_space: Any, tool_input: dict[str, Any] | None = None) -> str:
        """Classify a tool call's effect level.

        Returns: "read", "soft_write", "hard_write", or "unknown"
        Kernel tools have hardcoded classifications.
        MCP tools use tool_effects from CapabilityInfo.
        Unknown defaults to "hard_write" (safe default).
        """
        _KERNEL_READS = {"remember", "remember_details", "list_files", "read_file", "dismiss_whisper", "read_source", "read_doc", "read_soul", "manage_channels"}
        _KERNEL_WRITES = {"write_file", "delete_file", "manage_covenants", "update_soul", "manage_capabilities"}

        if tool_name in _KERNEL_READS:
            return "read"
        # manage_covenants: "list" is a read; other actions are writes
        if tool_name == "manage_covenants":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        # manage_capabilities: "list" is a read; other actions are writes
        if tool_name == "manage_capabilities":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        # manage_channels: "list" is a read; other actions are writes
        if tool_name == "manage_channels":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        # manage_schedule: all actions are read bypass. The gate only matters for
        # the scheduled ACTION at fire time (via covenants), not for managing the
        # schedule itself. The user explicitly asked to create/remove/pause — that's the intent.
        if tool_name == "manage_schedule":
            return "read"
        if tool_name in _KERNEL_WRITES:
            return "soft_write"

        if not self._registry:
            return "unknown"

        for cap in self._registry.get_all():
            if tool_name in (cap.tool_effects or {}):
                return cap.tool_effects[tool_name]
            if tool_name in (cap.tools or []) and tool_name not in (cap.tool_effects or {}):
                return "unknown"  # Tool exists but no effect declared

        return "unknown"  # Not found at all → safe default

    def _get_capability_for_tool(self, tool_name: str) -> str | None:
        """Return the capability name that owns this tool, or None."""
        if not self._registry:
            return None
        for cap in self._registry.get_all():
            if tool_name in (cap.tools or []):
                return cap.name
            if tool_name in (cap.tool_effects or {}):
                return cap.name
        return None

    def _get_tool_description(self, tool_name: str) -> str:
        """Return the tool's description from the MCP manifest, or empty string."""
        if self._mcp:
            try:
                for tool in self._mcp.get_tools():
                    if tool.get("name") == tool_name:
                        return tool.get("description", "")
            except Exception:
                pass
        return ""

    def _describe_action(self, tool_name: str, tool_input: dict) -> str:
        """Generate a human-readable description of a proposed tool call."""
        if tool_name == "create-event":
            summary = tool_input.get("summary", "an event")
            start = tool_input.get("start", "unspecified time")
            return f"Create calendar event: '{summary}' at {start}"
        if tool_name == "update-event":
            summary = tool_input.get("summary", "an event")
            return f"Update calendar event: '{summary}'"
        if tool_name == "delete-event":
            summary = tool_input.get("summary", "an event")
            return f"Delete calendar event: '{summary}'"
        if tool_name == "send-email":
            to = tool_input.get("to", "someone")
            subject = tool_input.get("subject", "no subject")
            return f"Send email to {to}: '{subject}'"
        if tool_name == "delete-email":
            msg_id = tool_input.get("id", "a message")
            return f"Delete email: {msg_id}"
        if tool_name == "delete_file":
            name = tool_input.get("name", "a file")
            return f"Delete file: {name}"
        if tool_name == "write_file":
            name = tool_input.get("name", "a file")
            return f"Write/update file: {name}"
        return f"Execute {tool_name} with {json.dumps(tool_input)[:200]}"

    async def _gate_tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        user_message: str,
        tenant_id: str,
        active_space_id: str,
        messages: list[dict] | None = None,
        approval_token_id: str | None = None,
        agent_reasoning: str = "",
    ) -> GateResult:
        """Authorization gate for write tool calls.

        Step 1: Approval token check (mechanical — user confirmed this specific action)
        Step 2: Permission override check (mechanical — capability set to always-allow)
        Step 3: Model evaluation — the correctness check (EXPLICIT/AUTHORIZED/CONFLICT/DENIED)

        Steps 1 and 2 are zero-cost mechanical bypasses. Step 3 is the only LLM call.
        Permission overrides are NOT included in rules_text — they bypass the model entirely.
        This ensures high-volume automation (50 emails, always-allow) doesn't trigger 50 model calls.
        """
        # Step 1: Approval token check (user confirmed this specific action previously)
        if approval_token_id and self._validate_approval_token(
            approval_token_id, tool_name, tool_input
        ):
            logger.info("GATE: token_validated tool=%s token=%s", tool_name, approval_token_id)
            return GateResult(allowed=True, reason="token_approved", method="token")

        # Step 2: Permission override (always-allow = zero-cost mechanical bypass, no model call)
        cap_name = self._get_capability_for_tool(tool_name)
        if cap_name and self._state:
            try:
                tenant = await self._state.get_tenant_profile(tenant_id)
                if tenant and tenant.permission_overrides.get(cap_name) == "always-allow":
                    logger.info("GATE: permission_override tool=%s cap=%s", tool_name, cap_name)
                    return GateResult(allowed=True, reason="permission_override", method="always_allow")
            except Exception as exc:
                logger.warning("Gate: permission override check failed: %s", exc)

        # Step 3: Model evaluation — the only LLM call
        return await self._evaluate_gate(
            tool_name, tool_input, effect, messages, agent_reasoning, tenant_id, active_space_id,
        )

    async def _evaluate_gate(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        messages: list[dict] | None,
        agent_reasoning: str,
        tenant_id: str,
        active_space_id: str,
    ) -> GateResult:
        """Step 2 of the dispatch gate: lightweight model evaluation.

        One LLM call. Sees everything. Returns EXPLICIT / AUTHORIZED / CONFLICT / DENIED.
        Permission overrides are included in rules_text so the model sees them too.
        """
        # Build recent_messages_text (last 5 user turns)
        recent_messages_text = "No recent messages."
        if messages:
            user_msgs = [m for m in messages if m.get("role") == "user"][-5:]
            if user_msgs:
                lines = []
                for m in user_msgs:
                    content = m.get("content", "")
                    if isinstance(content, str):
                        lines.append(f'- "{content[:300]}"')
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if text:
                            lines.append(f'- "{text[:300]}"')
                if lines:
                    recent_messages_text = "\n".join(lines)

        # Build rules_text: covenant rules + permission_overrides as [always-allow] entries
        rules_text = "No standing covenant rules."
        rules_count = 0
        must_not_rules: list[str] = []
        if self._state:
            try:
                rules = await self._state.query_covenant_rules(
                    tenant_id,
                    context_space_scope=[active_space_id, None],
                    active_only=True,
                )
                rule_lines = []
                for r in rules:
                    rule_lines.append(
                        f"- [{r.rule_type}] {r.description} (scope: {r.context_space or 'global'})"
                    )
                    if r.rule_type == "must_not":
                        must_not_rules.append(r.description)
                if rule_lines:
                    rules_count = len(rule_lines)
                    rules_text = "\n".join(rule_lines)
            except Exception as exc:
                logger.warning("Gate: covenant query failed: %s", exc)

        action_desc = self._describe_action(tool_name, tool_input)
        tool_description = self._get_tool_description(tool_name)

        system_prompt = (
            "You are a security gate checking whether an agent's proposed action is "
            "authorized. You have access to the user's recent messages, the agent's "
            "reasoning for the action, and the user's standing behavioral rules "
            "(covenants).\n\n"
            "Evaluate and answer with ONE of these:\n\n"
            "EXPLICIT — The user directly asked for this action in their recent messages.\n"
            "AUTHORIZED — A standing covenant rule explicitly covers this action, and "
            "the agent's reasoning is consistent with the evidence.\n"
            "CONFLICT: <exact rule text> — The user asked for this action, BUT a "
            "restriction (must_not rule) also applies. Copy the exact rule text after "
            "the colon. The user may be knowingly overriding the restriction.\n"
            "DENIED — The user did not ask for this, and no covenant authorizes it.\n\n"
            "Important:\n"
            "- If the user explicitly addresses a restriction (\"no need to review, "
            "just send it\"), that is an override — return EXPLICIT, not CONFLICT.\n"
            "- If the user asks for an action and a must_not rule exists but the user "
            "did NOT address the restriction, return CONFLICT: <that rule's exact text>.\n"
            "- Match the conflicting rule carefully — only flag a rule if it genuinely "
            "applies to the proposed action. Do not flag unrelated rules.\n"
            "- If the agent's reasoning claims the user asked for something but the "
            "recent messages don't support that claim, return DENIED.\n"
            "- When in doubt, return DENIED. It is always safe to ask.\n\n"
            "For CONFLICT, use format: CONFLICT: <rule text>\n"
            "For all others, answer with ONLY the one word."
        )
        user_content = (
            f"Recent user messages (oldest to newest):\n{recent_messages_text}\n\n"
            f"Agent's reasoning for this action:\n{agent_reasoning}\n\n"
            f"Proposed action: {tool_name}\n"
            f"Tool description: {tool_description}\n"
            f"Action details: {action_desc}\n\n"
            f"Active covenant rules:\n{rules_text}"
        )

        raw = ""
        logger.info("GATE_MODEL: max_tokens=512, has_schema=False, rules=%d", rules_count)
        try:
            raw = await self.complete_simple(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=512,
                prefer_cheap=True,
            )
        except Exception as exc:
            logger.warning("Gate: model evaluation failed: %s", exc)
        logger.info("GATE_MODEL: raw_response=%r", raw[:300])

        stripped = raw.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word == "EXPLICIT":
            return GateResult(
                allowed=True, reason="explicit_instruction", method="model_check",
                raw_response=raw,
            )
        if first_word == "AUTHORIZED":
            return GateResult(
                allowed=True, reason="covenant_authorized", method="model_check",
                raw_response=raw,
            )
        if first_word.startswith("CONFLICT"):
            # Extract rule text from "CONFLICT: <rule text>" format.
            # Fall back to must_not_rules[0] if the model didn't include it.
            conflicting_rule = ""
            if ":" in stripped:
                conflicting_rule = stripped.split(":", 1)[1].strip()
            if not conflicting_rule:
                conflicting_rule = must_not_rules[0] if must_not_rules else ""
            return GateResult(
                allowed=False, reason="covenant_conflict", method="model_check",
                proposed_action=action_desc, conflicting_rule=conflicting_rule, raw_response=raw,
            )
        # DENIED or anything unexpected
        return GateResult(
            allowed=False, reason="denied", method="model_check",
            proposed_action=action_desc, raw_response=raw,
        )

    def _issue_approval_token(self, tool_name: str, tool_input: dict) -> ApprovalToken:
        """Issue a single-use approval token for a blocked tool call."""
        token_id = uuid.uuid4().hex[:12]
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        token = ApprovalToken(
            token_id=token_id,
            tool_name=tool_name,
            tool_input_hash=input_hash,
            issued_at=datetime.now(timezone.utc),
        )
        self._approval_tokens[token_id] = token
        return token

    def _validate_approval_token(
        self, token_id: str, tool_name: str, tool_input: dict
    ) -> bool:
        """Validate an approval token. Marks it used on success.

        Returns True only if the token exists, is unused, is < 5 minutes old,
        matches the tool name, and the tool_input hash matches.
        """
        token = self._approval_tokens.get(token_id)
        if not token:
            return False
        if token.used:
            return False
        if token.tool_name != tool_name:
            return False
        age_seconds = (datetime.now(timezone.utc) - token.issued_at).total_seconds()
        if age_seconds > 300:  # 5-minute TTL
            return False
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        if token.tool_input_hash != input_hash:
            return False
        token.used = True
        return True

    async def execute_tool(
        self, tool_name: str, tool_input: dict, request: "ReasoningRequest"
    ) -> str:
        """Execute a tool call directly (used for confirmed pending actions).

        Handles both kernel tools and MCP tools. Mirrors the routing in reason().
        """
        if tool_name in self._KERNEL_TOOLS:
            if tool_name == "write_file":
                if self._files:
                    return await self._files.write_file(
                        request.tenant_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                        tool_input.get("content", ""),
                        tool_input.get("description", ""),
                    )
                return "File system is not available."
            elif tool_name == "read_file":
                if self._files:
                    return await self._files.read_file(
                        request.tenant_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "list_files":
                if self._files:
                    return await self._files.list_files(
                        request.tenant_id,
                        request.active_space_id,
                    )
                return "File system is not available."
            elif tool_name == "delete_file":
                if self._files:
                    return await self._files.delete_file(
                        request.tenant_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "remember":
                if self._retrieval:
                    return await self._retrieval.search(
                        request.tenant_id,
                        tool_input.get("query", ""),
                        request.active_space_id,
                    )
                return "Memory search is not available."
            elif tool_name == "dismiss_whisper":
                return await self._handle_dismiss_whisper(
                    request.tenant_id,
                    tool_input.get("whisper_id", ""),
                    tool_input.get("reason", "user_dismissed"),
                )
            elif tool_name == "read_source":
                return _read_source(
                    tool_input.get("path", ""),
                    tool_input.get("section", ""),
                )
            elif tool_name == "read_doc":
                return _read_doc(tool_input.get("path", ""))
            elif tool_name == "read_soul":
                if self._state:
                    soul = await self._state.get_soul(request.tenant_id)
                    if soul:
                        from dataclasses import asdict
                        return json.dumps(asdict(soul), indent=2)
                    return "No soul found for this tenant."
                return "State store is not available."
            elif tool_name == "update_soul":
                if self._state:
                    field = tool_input.get("field", "")
                    value = tool_input.get("value", "")
                    if field not in _SOUL_UPDATABLE_FIELDS:
                        return (
                            f"Cannot update '{field}'. Only these fields can be updated: "
                            f"{', '.join(sorted(_SOUL_UPDATABLE_FIELDS))}."
                        )
                    soul = await self._state.get_soul(request.tenant_id)
                    if not soul:
                        return "No soul found for this tenant."
                    setattr(soul, field, value)
                    await self._state.save_soul(soul, source="update_soul", trigger=f"{field}={value}")
                    return f"Updated {field} to: {value}"
                return "State store is not available."
            elif tool_name == "manage_covenants":
                from kernos.kernel.covenant_manager import handle_manage_covenants
                cov_action = tool_input.get("action", "list")
                cov_result = await handle_manage_covenants(
                    self._state,
                    request.tenant_id,
                    action=cov_action,
                    rule_id=tool_input.get("rule_id", ""),
                    new_description=tool_input.get("new_description", ""),
                    show_all=tool_input.get("show_all", False),
                )
                if cov_action == "update" and "Updated" in cov_result:
                    import asyncio, re
                    from kernos.kernel.covenant_manager import validate_covenant_set
                    id_match = re.search(r"new ID: (rule_\w+)", cov_result)
                    new_id = id_match.group(1) if id_match else ""
                    if new_id:
                        asyncio.create_task(
                            validate_covenant_set(
                                state=self._state,
                                events=self._events,
                                reasoning_service=self,
                                tenant_id=request.tenant_id,
                                new_rule_id=new_id,
                            )
                        )
                return cov_result
            elif tool_name == "manage_capabilities":
                return await self._handle_manage_capabilities(
                    request.tenant_id,
                    tool_input.get("action", "list"),
                    tool_input.get("capability", ""),
                )
            elif tool_name == "manage_channels":
                from kernos.kernel.channels import handle_manage_channels
                if self._channel_registry:
                    return handle_manage_channels(
                        self._channel_registry,
                        tool_input.get("action", "list"),
                        tool_input.get("channel", ""),
                    )
                return "Channel registry is not available."
            elif tool_name == "manage_schedule":
                from kernos.kernel.scheduler import handle_manage_schedule
                if self._trigger_store:
                    return await handle_manage_schedule(
                        self._trigger_store,
                        request.tenant_id,
                        member_id=request.active_space_id,
                        space_id=request.active_space_id,
                        action=tool_input.get("action", "list"),
                        trigger_id=tool_input.get("trigger_id", ""),
                        description=tool_input.get("description", ""),
                        reasoning_service=self,
                        conversation_id=request.conversation_id,
                    )
                return "Scheduler is not available."
            else:
                return f"Kernel tool '{tool_name}' not handled."
        else:
            return await self._mcp.call_tool(tool_name, tool_input)

    async def _handle_request_tool(
        self,
        tenant_id: str,
        space_id: str,
        capability_name: str,
        description: str,
    ) -> str:
        """Handle a request_tool call.

        1. If capability_name matches an installed capability: activate silently
        2. If capability_name is 'unknown': fuzzy match against registry using description
        3. If not installed: direct user to system space
        """
        from kernos.capability.registry import CapabilityStatus

        if not self._registry:
            return "Tool registry is not available right now."

        # Exact match (when capability_name is known)
        if capability_name and capability_name != "unknown":
            cap = self._registry.get(capability_name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                await self._activate_tool_for_space(tenant_id, space_id, capability_name)
                tools = cap.tools
                return (
                    f"Activated '{cap.name}' for this space. "
                    f"Available tools: {', '.join(tools)}. "
                    f"These will be available in this space going forward."
                )

        # Fuzzy match — check if any capability name or tool name appears in description
        desc_lower = description.lower()
        # Sort: universal first (prefer broadly useful tools)
        candidates = sorted(
            [c for c in self._registry.get_all() if c.status == CapabilityStatus.CONNECTED],
            key=lambda c: (not c.universal, c.name),
        )
        best_match = None
        for cap in candidates:
            if (cap.name.lower() in desc_lower or
                    any(tool.lower() in desc_lower for tool in cap.tools)):
                best_match = cap
                break

        if best_match:
            await self._activate_tool_for_space(tenant_id, space_id, best_match.name)
            tools = best_match.tools
            return (
                f"Found and activated '{best_match.name}' for this space. "
                f"Available tools: {', '.join(tools)}. "
                f"These will be available in this space going forward."
            )

        # Not installed
        return (
            f"I don't have a tool matching '{capability_name}' installed. "
            f"To get new tools set up, go to the System space for installation. "
            f"Want me to help you find the right tool there?"
        )

    async def _activate_tool_for_space(
        self, tenant_id: str, space_id: str, capability_name: str
    ) -> None:
        """Add a capability to a space's active_tools list and persist."""
        if not self._state:
            return
        space = await self._state.get_context_space(tenant_id, space_id)
        if space and capability_name not in space.active_tools:
            space.active_tools.append(capability_name)
            await self._state.update_context_space(
                tenant_id, space_id, {"active_tools": space.active_tools}
            )

    async def _handle_manage_capabilities(
        self, tenant_id: str, action: str, capability: str
    ) -> str:
        """Handle the manage_capabilities kernel tool."""
        from kernos.capability.registry import CapabilityStatus

        if not self._registry:
            return "Tool registry is not available right now."

        if action == "list":
            caps = self._registry.get_all()
            if not caps:
                return "No capabilities registered."
            lines = ["Capabilities:"]
            for cap in sorted(caps, key=lambda c: c.name):
                lines.append(
                    f"- {cap.name} ({cap.display_name}): "
                    f"status={cap.status.value}, source={cap.source}"
                )
                # Show individual tool names for connected capabilities
                if cap.tool_effects:
                    tool_names = ", ".join(sorted(cap.tool_effects.keys()))
                    lines.append(f"    Tools: {tool_names}")
            return "\n".join(lines)

        if action == "enable":
            if not capability:
                return "Error: 'capability' is required for enable."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.status == CapabilityStatus.CONNECTED:
                return f"'{capability}' is already enabled."
            if cap.status != CapabilityStatus.DISABLED:
                return (
                    f"Cannot enable '{capability}' — current status is "
                    f"'{cap.status.value}'. Only disabled capabilities can be enabled."
                )
            self._registry.enable(capability)
            self._tools_changed = True
            return f"Enabled '{capability}'. Its tools are now visible."

        if action == "disable":
            if not capability:
                return "Error: 'capability' is required for disable."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.status == CapabilityStatus.DISABLED:
                return f"'{capability}' is already disabled."
            if cap.status != CapabilityStatus.CONNECTED:
                return (
                    f"Cannot disable '{capability}' — current status is "
                    f"'{cap.status.value}'. Only connected capabilities can be disabled."
                )
            self._registry.disable(capability)
            self._tools_changed = True
            return (
                f"Disabled '{capability}'. Its tools are now hidden from the tool list. "
                f"The server is still running — re-enable will be instant."
            )

        if action == "install":
            if not capability:
                return "Error: 'capability' is required for install."
            # Route through request_tool for existing flow
            return await self._handle_request_tool(
                tenant_id, "", capability, f"Install {capability}"
            )

        if action == "remove":
            if not capability:
                return "Error: 'capability' is required for remove."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.source == "default":
                return (
                    f"Cannot remove '{capability}' — it's a pre-installed default. "
                    f"Use disable instead to hide it from the tool list."
                )
            # User-installed: disconnect and suppress
            if self._mcp and cap.status in (
                CapabilityStatus.CONNECTED, CapabilityStatus.DISABLED
            ):
                await self._mcp.disconnect_one(cap.server_name or capability)
            cap.status = CapabilityStatus.SUPPRESSED
            cap.tools = []
            self._tools_changed = True
            return f"Removed '{capability}'. It has been uninstalled."

        return f"Unknown action: '{action}'. Use list, enable, disable, install, or remove."

    async def _handle_dismiss_whisper(
        self, tenant_id: str, whisper_id: str, reason: str = "user_dismissed"
    ) -> str:
        """Dismiss a whisper — update suppression to prevent re-surfacing."""
        if not self._state:
            return "State store is not available."
        suppressions = await self._state.get_suppressions(
            tenant_id, whisper_id=whisper_id
        )
        if suppressions:
            s = suppressions[0]
            s.resolution_state = "dismissed"
            s.resolved_by = reason
            s.resolved_at = datetime.now(timezone.utc).isoformat()
            await self._state.save_suppression(tenant_id, s)
            return f"Dismissed whisper {whisper_id}. Won't bring this up again."
        return f"Whisper {whisper_id} not found in suppression registry."

    async def _handle_remember_details(
        self, tenant_id: str, space_id: str, input_data: dict,
    ) -> str:
        """Retrieve conversation text from a specific archived log file.

        Read-only. No state mutation.
        """
        source_ref = input_data.get("source_ref", "")
        query = input_data.get("query", "")

        if not source_ref:
            return (
                "No source reference provided. Call remember() first to find "
                "a Ledger entry with a source log reference (e.g., 'source: log_003'), "
                "then pass that reference here."
            )

        log_number = self._parse_log_ref(source_ref)
        if log_number is None:
            return (
                f"Could not parse '{source_ref}' as a log reference. "
                f"Expected format: 'log_003' or '3'. "
                f"Call remember() first to find the correct source reference."
            )

        # Read via public ConversationLogger API
        if not self._handler or not hasattr(self._handler, "conv_logger"):
            return "Conversation logger is not available."

        log_text = await self._handler.conv_logger.read_log_text(
            tenant_id, space_id, log_number,
        )

        if log_text is None:
            logger.info("DEEP_RECALL: space=%s log=%03d not_found", space_id, log_number)
            return f"Log file log_{log_number:03d} not found for this space."

        # If a query is provided, extract relevant section
        if query:
            relevant = self._extract_relevant_section(log_text, query)
            if relevant:
                logger.info(
                    "DEEP_RECALL: space=%s log=%03d query=%s chars=%d",
                    space_id, log_number, query[:50], len(relevant),
                )
                return (
                    f"From log_{log_number:03d} — section matching '{query}':"
                    f"\n\n{relevant}"
                )
            else:
                return (
                    f"Log_{log_number:03d} exists but no section matches '{query}'. "
                    f"Try a different search term, or omit the query to see the full log."
                )

        # No query — return bounded log content
        max_chars = 8000  # ~2000 tokens
        if len(log_text) <= max_chars:
            logger.info(
                "DEEP_RECALL: space=%s log=%03d full chars=%d",
                space_id, log_number, len(log_text),
            )
            return f"From log_{log_number:03d} (full log):\n\n{log_text}"

        # Log too large — head + tail with gap notice
        chunk_size = max_chars // 2
        head = log_text[:chunk_size]
        tail = log_text[-chunk_size:]
        logger.info(
            "DEEP_RECALL: space=%s log=%03d bounded chars=%d (total=%d)",
            space_id, log_number, max_chars, len(log_text),
        )
        return (
            f"From log_{log_number:03d} ({len(log_text)} chars total, "
            f"showing first and last sections):\n\n"
            f"--- START ---\n{head}\n\n"
            f"--- GAP ({len(log_text) - max_chars} chars omitted) ---\n\n"
            f"--- END ---\n{tail}\n\n"
            f"To see a specific section, retry with a query keyword."
        )

    @staticmethod
    def _parse_log_ref(ref: str) -> int | None:
        """Parse a log reference string into a log number.

        Accepts: "log_003", "log_3", "3", "log003"
        """
        import re as _re
        match = _re.match(r'log_?(\d+)', ref.strip().lower())
        if match:
            return int(match.group(1))
        try:
            return int(ref.strip())
        except ValueError:
            return None

    @staticmethod
    def _extract_relevant_section(
        log_text: str, query: str, context_lines: int = 10,
    ) -> str:
        """Extract lines from a log relevant to a query.

        Simple keyword matching with surrounding context lines.
        """
        lines = log_text.split("\n")
        query_lower = query.lower()

        matching_indices = [
            i for i, line in enumerate(lines) if query_lower in line.lower()
        ]

        if not matching_indices:
            return ""

        included: set[int] = set()
        for idx in matching_indices:
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            for i in range(start, end):
                included.add(i)

        return "\n".join(lines[i] for i in sorted(included))

    async def reason(self, request: ReasoningRequest) -> ReasoningResult:
        """Run a full reasoning turn, including tool-use loop.

        Raises ReasoningError subtypes on provider failure. Does NOT catch them.
        """
        t_global = time.monotonic()
        messages = list(request.messages)
        tools = request.tools
        total_input_tokens = 0
        total_output_tokens = 0

        # --- Initial reasoning.request ---
        try:
            await emit_event(
                self._events,
                EventType.REASONING_REQUEST,
                request.tenant_id,
                "reasoning_service",
                payload={
                    "model": request.model,
                    "provider": getattr(self._provider, "provider_name", "unknown"),
                    "conversation_id": request.conversation_id,
                    "message_count": len(messages),
                    "tool_count": len(tools),
                    "system_prompt_length": len(request.system_prompt),
                    "trigger": request.trigger,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit reasoning.request: %s", exc)

        # Estimate context size: rough token count of system prompt + messages + tools.
        # 1 token ≈ 4 chars is a reasonable approximation for English prose.
        _tool_chars = sum(len(json.dumps(t)) for t in tools)
        _ctx_chars = len(request.system_prompt) + sum(
            len(m.get("content", "") if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", "")))
            for m in messages
        )
        _ctx_tokens_est = (_ctx_chars + _tool_chars) // 4
        _tool_sizes = [(t.get("name", "?"), len(json.dumps(t))) for t in tools]
        _tool_sizes.sort(key=lambda x: x[1], reverse=True)
        _top3 = ", ".join(f"{name}={chars//4}tok" for name, chars in _tool_sizes[:3])
        logger.info(
            "REASON_START: tool_count=%d max_tokens=%d msg_count=%d "
            "ctx_tokens_est=%d (msg=%d tools=%d) top_tools=[%s]",
            len(tools), request.max_tokens, len(messages), _ctx_tokens_est,
            _ctx_chars // 4, _tool_chars // 4, _top3,
        )
        if logger.isEnabledFor(logging.DEBUG):
            for t in tools:
                _t_json = json.dumps(t)
                logger.debug(
                    "TOOL_SIZE: name=%s tokens_est=%d chars=%d",
                    t.get("name", "unknown"),
                    len(_t_json) // 4,
                    len(_t_json),
                )

        logger.info(
            "LLM_REQUEST: messages=%d tools=%d max_tokens=%d",
            len(messages), len(tools), request.max_tokens,
        )
        t0 = time.monotonic()
        response = await self._provider.complete(
            model=request.model,
            system=request.system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=request.max_tokens,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        logger.info(
            "LLM_RESPONSE: stop_reason=%s tokens_in=%d tokens_out=%d content_types=%s",
            response.stop_reason,
            response.input_tokens, response.output_tokens,
            [b.type for b in response.content],
        )
        if response.cache_creation_input_tokens or response.cache_read_input_tokens:
            logger.info(
                "CACHE: write=%d read=%d",
                response.cache_creation_input_tokens,
                response.cache_read_input_tokens,
            )
        for _b in response.content:
            if _b.type == "text":
                logger.info(
                    "LLM_BLOCK: type=text len=%d preview=%r",
                    len(_b.text or ""), (_b.text or "")[:300],
                )
            elif _b.type == "tool_use":
                logger.info(
                    "LLM_BLOCK: type=tool_use name=%s input=%r",
                    _b.name, str(_b.input)[:300],
                )

        rr_event = None
        try:
            rr_event = await emit_event(
                self._events,
                EventType.REASONING_RESPONSE,
                request.tenant_id,
                "reasoning_service",
                payload={
                    "model": request.model,
                    "provider": getattr(self._provider, "provider_name", "unknown"),
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "estimated_cost_usd": estimate_cost(
                        request.model, response.input_tokens, response.output_tokens
                    ),
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    "conversation_id": request.conversation_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit reasoning.response: %s", exc)

        # Log what the model actually returned — before entering the tool-use loop.
        # This is the key diagnostic: did the model produce tool_use blocks or just text?
        _content_types = [block.type for block in response.content]
        _text_preview = ""
        for _b in response.content:
            if _b.type == "text" and _b.text:
                _text_preview = _b.text[:200]
                break
        logger.info(
            "REASON_RESPONSE: stop=%s content_types=%s text_preview=%r",
            response.stop_reason,
            _content_types,
            _text_preview,
        )

        # --- Tool-use loop ---
        iterations = 0
        _gate_cache: dict[str, Any] = {}  # tool_name → GateResult (for lazy-load re-runs)
        while (
            response.stop_reason == "tool_use"
            and iterations < self.MAX_TOOL_ITERATIONS
        ):
            iterations += 1
            tool_results = []

            # Build a per-tool-call index of agent reasoning.
            # For each tool_use block: the most recent text block immediately before it.
            # If there's no text block before a tool_use, use "No explicit reasoning provided."
            _last_text = "No explicit reasoning provided."
            _tool_reasoning: dict[str, str] = {}
            for _b in response.content:
                if _b.type == "text" and _b.text:
                    _last_text = _b.text.strip() or "No explicit reasoning provided."
                elif _b.type == "tool_use" and _b.id:
                    _tool_reasoning[_b.id] = _last_text
                    _last_text = "No explicit reasoning provided."  # reset for next tool call

            for block in response.content:
                if block.type != "tool_use":
                    continue

                agent_reasoning = _tool_reasoning.get(block.id or "", "No explicit reasoning provided.")

                logger.info(
                    "TOOL_LOOP iter=%d tool=%s kernel=%s",
                    iterations, block.name, block.name in self._KERNEL_TOOLS,
                )

                # Extract and clean tool_input — pop _approval_token before gate or exec
                tool_input = dict(block.input or {})
                approval_token_id = tool_input.pop("_approval_token", None)

                # Console logging: every tool call the agent makes
                logger.info(
                    "AGENT_ACTION: tool=%s input=%s",
                    block.name, json.dumps(tool_input)[:200],
                )

                # Dispatch Gate: classify and check write tools before execution
                tool_effect = self._classify_tool_effect(block.name, request.active_space, tool_input)
                if tool_effect in ("soft_write", "hard_write", "unknown"):
                    # Check gate cache (lazy-load re-runs skip redundant gate evaluation)
                    if block.name in _gate_cache and _gate_cache[block.name].allowed:
                        gate_result = _gate_cache.pop(block.name)
                        logger.info(
                            "GATE_CACHED: tool=%s (approved on stub call)", block.name,
                        )
                    else:
                        gate_result = await self._gate_tool_call(
                            block.name, tool_input, tool_effect,
                            request.input_text, request.tenant_id,
                            request.active_space_id,
                            messages=request.messages,
                            approval_token_id=approval_token_id,
                            agent_reasoning=agent_reasoning,
                        )

                    try:
                        await emit_event(
                            self._events,
                            EventType.DISPATCH_GATE,
                            request.tenant_id,
                            "dispatch_interceptor",
                            payload={
                                "tool_name": block.name,
                                "effect": tool_effect,
                                "allowed": gate_result.allowed,
                                "reason": gate_result.reason,
                                "method": gate_result.method,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to emit dispatch.gate: %s", exc)

                    logger.info(
                        "GATE: tool=%s effect=%s allowed=%s reason=%s method=%s",
                        block.name, tool_effect, gate_result.allowed,
                        gate_result.reason, gate_result.method,
                    )

                    if not gate_result.allowed:
                        # Keep token for programmatic callers (Step 1 of the gate)
                        self._issue_approval_token(block.name, tool_input)
                        # Store PendingAction for kernel-owned replay
                        tenant_id = request.tenant_id
                        if tenant_id not in self._pending_actions:
                            self._pending_actions[tenant_id] = []
                        pending_idx = len(self._pending_actions[tenant_id])
                        self._pending_actions[tenant_id].append(PendingAction(
                            tool_name=block.name,
                            tool_input=dict(tool_input),
                            proposed_action=gate_result.proposed_action,
                            conflicting_rule=gate_result.conflicting_rule,
                            gate_reason=gate_result.reason,
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                        ))
                        if gate_result.reason == "covenant_conflict":
                            system_msg = (
                                f"[SYSTEM] Action blocked — conflict with standing rule. "
                                f"Proposed: {gate_result.proposed_action}. "
                                f"Conflicting rule: {gate_result.conflicting_rule}. "
                                f"Pending action index: {pending_idx}. "
                                f"Ask the user to confirm. If they confirm, include "
                                f"[CONFIRM:{pending_idx}] in your response. "
                                f"Also offer three options: "
                                f"1. Respect the rule (don't do it). "
                                f"2. Override this time (confirm the action). "
                                f"3. Update the rule permanently."
                            )
                        else:
                            system_msg = (
                                f"[SYSTEM] Action blocked — no authorization found. "
                                f"Proposed: {gate_result.proposed_action}. "
                                f"Pending action index: {pending_idx}. "
                                f"Ask the user if they want to proceed. If they confirm, "
                                f"include [CONFIRM:{pending_idx}] in your response. "
                                f"You may also offer to create a standing rule."
                            )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": system_msg,
                        })
                        continue

                # Emit tool.called
                try:
                    await emit_event(
                        self._events,
                        EventType.TOOL_CALLED,
                        request.tenant_id,
                        "reasoning_service",
                        payload={
                            "tool_name": block.name,
                            "tool_input": tool_input,
                            "conversation_id": request.conversation_id,
                            "reasoning_event_id": rr_event.id if rr_event else None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit tool.called: %s", exc)

                await self._audit.log(
                    request.tenant_id,
                    {
                        "type": "tool_call",
                        "timestamp": _now_iso(),
                        "tenant_id": request.tenant_id,
                        "conversation_id": request.conversation_id,
                        "tool_name": block.name,
                        "tool_input": tool_input,
                    },
                )

                t_tool = time.monotonic()
                # Kernel tool routing: remember + file tools handled internally
                if block.name in self._KERNEL_TOOLS:
                    logger.info(
                        "KERNEL_TOOL name=%s space=%s",
                        block.name, request.active_space_id,
                    )
                    tool_args = tool_input
                    if block.name == "remember":
                        if self._retrieval:
                            try:
                                result = await self._retrieval.search(
                                    request.tenant_id,
                                    tool_args.get("query", ""),
                                    request.active_space_id,
                                )
                            except Exception as exc:
                                logger.warning("Kernel tool 'remember' failed: %s", exc)
                                result = "Memory search failed — try asking in a different way."
                        else:
                            result = "Memory search is not available right now."
                    elif block.name == "remember_details":
                        result = await self._handle_remember_details(
                            request.tenant_id,
                            request.active_space_id,
                            tool_args,
                        )
                    elif block.name == "write_file":
                        if self._files:
                            try:
                                result = await self._files.write_file(
                                    request.tenant_id,
                                    request.active_space_id,
                                    tool_args.get("name", ""),
                                    tool_args.get("content", ""),
                                    tool_args.get("description", ""),
                                )
                            except Exception as exc:
                                logger.warning("Kernel tool 'write_file' failed: %s", exc)
                                result = "File write failed — try again."
                        else:
                            result = "File system is not available right now."
                    elif block.name == "read_file":
                        if self._files:
                            try:
                                result = await self._files.read_file(
                                    request.tenant_id,
                                    request.active_space_id,
                                    tool_args.get("name", ""),
                                )
                            except Exception as exc:
                                logger.warning("Kernel tool 'read_file' failed: %s", exc)
                                result = "File read failed — try again."
                        else:
                            result = "File system is not available right now."
                    elif block.name == "list_files":
                        if self._files:
                            try:
                                result = await self._files.list_files(
                                    request.tenant_id,
                                    request.active_space_id,
                                )
                            except Exception as exc:
                                logger.warning("Kernel tool 'list_files' failed: %s", exc)
                                result = "File listing failed — try again."
                        else:
                            result = "File system is not available right now."
                    elif block.name == "delete_file":
                        if self._files:
                            try:
                                result = await self._files.delete_file(
                                    request.tenant_id,
                                    request.active_space_id,
                                    tool_args.get("name", ""),
                                )
                            except Exception as exc:
                                logger.warning("Kernel tool 'delete_file' failed: %s", exc)
                                result = "File deletion failed — try again."
                        else:
                            result = "File system is not available right now."
                    elif block.name == "dismiss_whisper":
                        try:
                            result = await self._handle_dismiss_whisper(
                                request.tenant_id,
                                tool_args.get("whisper_id", ""),
                                tool_args.get("reason", "user_dismissed"),
                            )
                        except Exception as exc:
                            logger.warning("Kernel tool 'dismiss_whisper' failed: %s", exc)
                            result = "Failed to dismiss whisper — try again."
                    elif block.name == "read_source":
                        result = _read_source(
                            tool_args.get("path", ""),
                            tool_args.get("section", ""),
                        )
                    elif block.name == "read_doc":
                        result = _read_doc(tool_args.get("path", ""))
                    elif block.name == "read_soul":
                        if self._state:
                            soul = await self._state.get_soul(request.tenant_id)
                            if soul:
                                from dataclasses import asdict
                                result = json.dumps(asdict(soul), indent=2)
                            else:
                                result = "No soul found for this tenant."
                        else:
                            result = "State store is not available."
                    elif block.name == "update_soul":
                        if self._state:
                            field = tool_args.get("field", "")
                            value = tool_args.get("value", "")
                            if field not in _SOUL_UPDATABLE_FIELDS:
                                result = (
                                    f"Cannot update '{field}'. Only these fields can be updated: "
                                    f"{', '.join(sorted(_SOUL_UPDATABLE_FIELDS))}."
                                )
                            else:
                                soul = await self._state.get_soul(request.tenant_id)
                                if not soul:
                                    result = "No soul found for this tenant."
                                else:
                                    setattr(soul, field, value)
                                    await self._state.save_soul(soul, source="update_soul", trigger=f"{field}={value}")
                                    result = f"Updated {field} to: {value}"
                        else:
                            result = "State store is not available."
                    elif block.name == "manage_covenants":
                        try:
                            from kernos.kernel.covenant_manager import handle_manage_covenants
                            cov_action = tool_args.get("action", "list")
                            result = await handle_manage_covenants(
                                self._state,
                                request.tenant_id,
                                action=cov_action,
                                rule_id=tool_args.get("rule_id", ""),
                                new_description=tool_args.get("new_description", ""),
                                show_all=tool_args.get("show_all", False),
                            )
                            # Fire post-write validation after update (not remove)
                            if cov_action == "update" and "Updated" in result:
                                import asyncio
                                from kernos.kernel.covenant_manager import validate_covenant_set
                                # Extract new_id from result text
                                import re
                                id_match = re.search(r"new ID: (rule_\w+)", result)
                                new_id = id_match.group(1) if id_match else ""
                                if new_id:
                                    asyncio.create_task(
                                        validate_covenant_set(
                                            state=self._state,
                                            events=self._events,
                                            reasoning_service=self,
                                            tenant_id=request.tenant_id,
                                            new_rule_id=new_id,
                                        )
                                    )
                        except Exception as exc:
                            logger.warning("Kernel tool 'manage_covenants' failed: %s", exc)
                            result = "Failed to manage covenants — try again."
                    elif block.name == "manage_capabilities":
                        try:
                            result = await self._handle_manage_capabilities(
                                request.tenant_id,
                                tool_args.get("action", "list"),
                                tool_args.get("capability", ""),
                            )
                        except Exception as exc:
                            logger.warning("Kernel tool 'manage_capabilities' failed: %s", exc)
                            result = "Failed to manage tools — try again."
                    elif block.name == "manage_channels":
                        from kernos.kernel.channels import handle_manage_channels
                        if self._channel_registry:
                            result = handle_manage_channels(
                                self._channel_registry,
                                tool_args.get("action", "list"),
                                tool_args.get("channel", ""),
                            )
                        else:
                            result = "Channel registry is not available."
                    elif block.name == "manage_schedule":
                        from kernos.kernel.scheduler import handle_manage_schedule
                        if self._trigger_store:
                            result = await handle_manage_schedule(
                                self._trigger_store,
                                request.tenant_id,
                                member_id=request.active_space_id,
                                space_id=request.active_space_id,
                                action=tool_args.get("action", "list"),
                                trigger_id=tool_args.get("trigger_id", ""),
                                description=tool_args.get("description", ""),
                                reasoning_service=self,
                                conversation_id=request.conversation_id,
                            )
                        else:
                            result = "Scheduler is not available."
                    else:
                        result = f"Kernel tool '{block.name}' not handled."
                else:
                    # Lazy tool loading: check if this tool is a stub (loads on first use).
                    # A stub has "additionalProperties": true and empty properties — the agent
                    # generated a call with best-guess params. Load full schema and re-run.
                    _is_stub = False
                    _tool_entry = None
                    for _t in tools:
                        if _t.get("name") == block.name:
                            _tool_entry = _t
                            break
                    if _tool_entry:
                        _schema = _tool_entry.get("input_schema", {})
                        _is_stub = (
                            _schema.get("additionalProperties") is True
                            and not _schema.get("properties")
                        )

                    if _is_stub and self._registry:
                        full_schema = self._registry.get_tool_schema(block.name)
                        if full_schema:
                            # Replace stub with full schema in the tools list
                            for _i, _t in enumerate(tools):
                                if _t.get("name") == block.name:
                                    tools[_i] = full_schema
                                    break
                            self.load_tool(request.active_space_id, block.name)
                            # Cache gate result so re-run doesn't re-evaluate
                            try:
                                if tool_effect in ("soft_write", "hard_write", "unknown"):
                                    _gate_cache[block.name] = gate_result
                            except NameError:
                                pass  # gate_result not set (read tool)
                            logger.info(
                                "TOOL_LOAD: tool=%s space=%s (stub -> full schema, re-running)",
                                block.name, request.active_space_id,
                            )
                            # Return a tool result asking the agent to retry with full schema
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": (
                                    f"[SYSTEM] The tool {block.name} is now fully loaded. "
                                    "Please retry your call with the correct parameters."
                                ),
                            })
                            continue  # Skip execution — agent will retry with full schema

                    if not _tool_entry and self._registry:
                        # Tool not in list at all — check if it exists in registry
                        schema = self._registry.get_tool_schema(block.name)
                        if schema:
                            self.load_tool(request.active_space_id, block.name)
                            tools.append(schema)
                            logger.info(
                                "TOOL_LOAD: tool=%s space=%s (not in list, schema loaded)",
                                block.name, request.active_space_id,
                            )
                        else:
                            result = f"Tool '{block.name}' is not available."
                            tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
                            logger.info(
                                "AGENT_RESULT: tool=%s success=%s preview=%s",
                                block.name, False, result[:100],
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })
                            continue
                    result = await self._mcp.call_tool(block.name, tool_input)
                tool_duration_ms = int((time.monotonic() - t_tool) * 1000)

                is_error = result.startswith("Tool error:") or result.startswith(
                    "Calendar tool error:"
                )

                # Console logging: tool result
                logger.info(
                    "AGENT_RESULT: tool=%s success=%s preview=%s",
                    block.name, not is_error, result[:100],
                )

                # Emit tool.result
                try:
                    await emit_event(
                        self._events,
                        EventType.TOOL_RESULT,
                        request.tenant_id,
                        "reasoning_service",
                        payload={
                            "tool_name": block.name,
                            "success": not is_error,
                            "result_length": len(result),
                            "duration_ms": tool_duration_ms,
                            "conversation_id": request.conversation_id,
                            "error": result if is_error else None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit tool.result: %s", exc)

                await self._audit.log(
                    request.tenant_id,
                    {
                        "type": "tool_result",
                        "timestamp": _now_iso(),
                        "tenant_id": request.tenant_id,
                        "conversation_id": request.conversation_id,
                        "tool_name": block.name,
                        "tool_output": str(result)[:2000],
                    },
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_api_dict(b) for b in response.content],
                }
            )
            messages.append({"role": "user", "content": tool_results})

            # Emit reasoning.request for continuation
            try:
                await emit_event(
                    self._events,
                    EventType.REASONING_REQUEST,
                    request.tenant_id,
                    "reasoning_service",
                    payload={
                        "model": request.model,
                        "provider": getattr(self._provider, "provider_name", "unknown"),
                        "conversation_id": request.conversation_id,
                        "message_count": len(messages),
                        "tool_count": len(tools),
                        "system_prompt_length": len(request.system_prompt),
                        "trigger": "tool_continuation",
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit reasoning.request: %s", exc)

            logger.info(
                "LLM_REQUEST: messages=%d tools=%d max_tokens=%d",
                len(messages), len(tools), request.max_tokens,
            )
            t0 = time.monotonic()
            response = await self._provider.complete(
                model=request.model,
                system=request.system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=request.max_tokens,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens

            logger.info(
                "LLM_RESPONSE: stop_reason=%s content_types=%s",
                response.stop_reason,
                [b.type for b in response.content],
            )
            if response.cache_creation_input_tokens or response.cache_read_input_tokens:
                logger.info(
                    "CACHE: write=%d read=%d",
                    response.cache_creation_input_tokens,
                    response.cache_read_input_tokens,
                )
            for _b in response.content:
                if _b.type == "text":
                    logger.info(
                        "LLM_BLOCK: type=text len=%d preview=%r",
                        len(_b.text or ""), (_b.text or "")[:300],
                    )
                elif _b.type == "tool_use":
                    logger.info(
                        "LLM_BLOCK: type=tool_use name=%s input=%r",
                        _b.name, str(_b.input)[:300],
                    )

            rr_event = None
            try:
                rr_event = await emit_event(
                    self._events,
                    EventType.REASONING_RESPONSE,
                    request.tenant_id,
                    "reasoning_service",
                    payload={
                        "model": request.model,
                        "provider": getattr(self._provider, "provider_name", "unknown"),
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "estimated_cost_usd": estimate_cost(
                            request.model, response.input_tokens, response.output_tokens
                        ),
                        "stop_reason": response.stop_reason,
                        "duration_ms": duration_ms,
                        "conversation_id": request.conversation_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit reasoning.response: %s", exc)

        # --- Build result ---
        total_duration_ms = int((time.monotonic() - t_global) * 1000)
        estimated_cost = estimate_cost(
            request.model, total_input_tokens, total_output_tokens
        )

        logger.info(
            "TOOL_LOOP exit: iterations=%d stop=%s has_text=%s",
            iterations, response.stop_reason,
            bool([b for b in response.content if b.type == "text"]),
        )

        if response.stop_reason == "max_tokens":
            logger.warning(
                "RESPONSE_TRUNCATED: max_tokens=%d reached on iter=%d. "
                "Tool calls may have been cut off. Consider raising max_tokens.",
                request.max_tokens, iterations,
            )

        if iterations >= self.MAX_TOOL_ITERATIONS:
            logger.warning("TOOL_LOOP EXHAUSTED after %d iterations", iterations)
            return ReasoningResult(
                text="I'm having trouble completing that request. Try asking in a simpler way.",
                model=request.model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                estimated_cost_usd=estimated_cost,
                duration_ms=total_duration_ms,
                tool_iterations=iterations,
            )

        text_parts = [b.text for b in response.content if b.type == "text"]
        response_text = (
            "".join(text_parts)
            if text_parts
            else "I processed your request but don't have a text response. Try rephrasing?"
        )

        # Hallucination detector: observe-only mode.
        # Detects when the agent claims tool use in text without actually calling a tool.
        # Logs detection + Haiku analysis for monitoring. Does NOT intervene or block.
        if iterations == 0 and response.stop_reason == "end_turn":
            _TOOL_CLAIM_PHRASES = (
                # Agent claims it used a specific tool
                "used write_file", "used delete_file", "used read_file",
                "used list_files", "used create-event", "used send-email",
                "used manage_schedule", "used remember",
                # Agent claims it performed an action (subject + past tense)
                "i created", "i deleted", "i wrote", "i removed",
                "i've created", "i've deleted", "i've written", "i've removed",
                "i scheduled", "i've scheduled", "i set a reminder",
                "i've set a reminder", "i'll remind",
                # Completion claims at start of response
                "done —", "✅",
                # Schedule-specific: agent describes timing of a future action
                "scheduled —", "fires at",
                "incoming at", "coming at", "will fire at",
                "set for", "reminder set", "you'll get",
                "heads up at", "alert at", "notification at",
                "locked in", "lands at", "queued for",
                "on its way", "will arrive at", "dropping at",
            )
            rt_lower = response_text.lower()

            # Pattern-based detection: if the user's message implied an action request
            # and the agent responded with text-only (no tool call), check via Haiku.
            # This catches novel phrasing that the phrase list misses.
            _phrase_match = any(phrase in rt_lower for phrase in _TOOL_CLAIM_PHRASES)
            if not _phrase_match and request.input_text:
                _ACTION_REQUEST_SIGNALS = (
                    "remind", "schedule", "set a", "create a", "send a",
                    "send me", "delete", "remove", "write a", "save",
                    "remember", "tell me to", "notify me",
                    "in 2 min", "in 5 min", "in 10 min", "in an hour",
                    "in 1 hour", "in 30 min", "in 15 min",
                    "tomorrow at", "every morning", "every day",
                )
                _input_lower = request.input_text.lower()
                _user_wants_action = any(
                    sig in _input_lower for sig in _ACTION_REQUEST_SIGNALS
                )
                if _user_wants_action and len(response_text) < 200:
                    import re
                    _has_time = bool(re.search(r'\d{1,2}:\d{2}', response_text))
                    if _has_time:
                        _phrase_match = True
                        logger.info(
                            "HALLUCINATION_PATTERN: short response with time to action "
                            "request, no tool call. input=%r response=%r",
                            request.input_text[:100], response_text[:100],
                        )

            if _phrase_match:
                original_preview = response_text[:200]
                logger.warning(
                    "HALLUCINATION_CHECK: Agent claims tool use but iterations=0 "
                    "(stop=%s, tool_count=%d). Hands-off mode — response passed through. "
                    "Original: %s",
                    response.stop_reason, len(tools), original_preview,
                )

                # Analyze why this hallucination occurred (cheap Haiku call for diagnostics)
                try:
                    _user_msg = request.input_text[:200] if request.input_text else "(unknown)"
                    _expected_tool = "unknown"
                    for _phrase, _tool in (
                        ("schedul", "manage_schedule"), ("remind", "manage_schedule"),
                        ("calendar", "create-event"), ("event", "create-event"),
                        ("email", "send-email"), ("file", "write_file"),
                        ("remember", "remember"),
                    ):
                        if _phrase in rt_lower:
                            _expected_tool = _tool
                            break
                    _prior_calls = sum(
                        1 for m in messages
                        if m.get("role") == "assistant"
                        and isinstance(m.get("content"), list)
                        and any(
                            b.get("type") == "tool_use" and b.get("name") == _expected_tool
                            for b in m["content"]
                            if isinstance(b, dict)
                        )
                    )
                    _analysis = await self.complete_simple(
                        system_prompt=(
                            "You are analyzing why an LLM agent generated text "
                            "instead of calling a tool. Be specific about what "
                            "in the context most likely caused this."
                        ),
                        user_content=(
                            f"The agent was asked: {_user_msg}\n"
                            f"Available tool: {_expected_tool}\n"
                            f"Instead of calling the tool, it generated: "
                            f"'{original_preview}'\n"
                            f"Conversation had {len(messages)} messages. "
                            f"Prior successful calls to this tool in session: "
                            f"{_prior_calls}\n"
                            f"Analyze the most likely cause in 1-2 sentences."
                        ),
                        max_tokens=256,
                        prefer_cheap=True,
                    )
                    logger.info(
                        "HALLUCINATION_ANALYSIS: tool=%s prior_calls=%d analysis=%s",
                        _expected_tool, _prior_calls, _analysis[:200],
                    )
                except Exception as _exc:
                    logger.warning("HALLUCINATION_ANALYSIS: failed: %s", _exc)

                # COACHING LOOP: Currently disabled (hands-off mode).
                # The system prompt cleanup (ACTIONS REQUIRE TOOL CALLS,
                # bootstrap graduation, covenant pruning) was the real fix.
                # Re-enable if hallucination rates increase after prompt
                # changes stabilize.
                # See: Session 2026-03-22, hallucination audit findings.

        return ReasoningResult(
            text=response_text,
            model=request.model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimated_cost,
            duration_ms=total_duration_ms,
            tool_iterations=iterations,
        )
