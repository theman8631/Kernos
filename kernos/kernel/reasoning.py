"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
    ) -> ProviderResponse:
        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            create_kwargs["tools"] = tools
        if output_schema:
            create_kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }

        try:
            response = self._client.messages.create(**create_kwargs)
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
        return ProviderResponse(
            content=content,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


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
    max_tokens: int = 1024
    active_space_id: str = ""  # For kernel tool routing (e.g., remember)
    input_text: str = ""       # Current user message — used by dispatch gate
    active_space: Any = None   # ContextSpace | None — for gate tool effect classification


@dataclass
class GateResult:
    """The outcome of a dispatch gate check."""

    allowed: bool
    reason: str    # "explicit_instruction", "permission_override", "covenant_authorized",
                   # "covenant_denied", "covenant_ambiguous", "no_covenants", "no_authorization"
    method: str    # "fast_path", "always_allow", "haiku_check", "ask_user", "none"
    proposed_action: str = ""  # Human-readable description of what was blocked


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

    async def complete_simple(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 512,
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
        model = _CHEAP_MODEL if prefer_cheap else _SIMPLE_MODEL
        response = await self._provider.complete(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=[],
            max_tokens=max_tokens,
            output_schema=output_schema,
        )
        if response.stop_reason == "max_tokens":
            logger.warning("complete_simple: response truncated (max_tokens reached)")
            return "{}"
        if response.stop_reason == "refusal":
            logger.warning("complete_simple: response refused by model")
            return "{}"
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "".join(text_parts)

    # Kernel tools: intercepted before MCP, never passed through to external servers
    _KERNEL_TOOLS = {"remember", "write_file", "read_file", "list_files", "delete_file", "request_tool"}

    # ---------------------------------------------------------------------------
    # Dispatch Gate (SPEC-3D)
    # ---------------------------------------------------------------------------

    _TOOL_SIGNALS: dict[str, list[str]] = {
        # Calendar writes
        "create-event": ["schedule", "book", "set up", "add to calendar", "make an appointment",
                         "create event", "put on my calendar", "block time", "add meeting"],
        "update-event": ["reschedule", "move", "change", "update event", "push back", "move to"],
        "delete-event": ["cancel", "remove event", "delete event", "take off calendar"],
        # Email writes
        "send-email": ["send", "email", "write to", "reply to", "forward"],
        "delete-email": ["delete email", "trash", "remove email"],
        # File writes (kernel tools)
        "write_file": ["create file", "write", "save", "draft"],
        "delete_file": ["delete", "remove", "get rid of", "trash", "clean up",
                        "clear out", "throw away", "discard", "drop", "nuke", "wipe", "erase"],
    }

    _CONFIRMATION_SIGNALS: list[str] = [
        "yes", "go ahead", "do it", "proceed", "confirmed", "approve",
        "that's fine", "ok", "sure", "yep", "yeah",
    ]

    # Phrases in an assistant message that indicate a previously blocked action
    _BLOCKED_CONTEXT_INDICATORS: list[str] = [
        "don't have permission",
        "need your permission",
        "should i go ahead",
        "should i proceed",
        "need explicit",
        "haven't authorized",
        "would you like me to",
        "waiting for your",
        "permission before",
        "your approval",
        "explicit instruction",
    ]

    # Domain keywords for matching must_not covenants against tool names
    _DOMAIN_KEYWORDS: dict[str, list[str]] = {
        "create-event": ["calendar", "event", "schedule", "meeting", "appointment"],
        "update-event": ["calendar", "event", "schedule", "meeting"],
        "delete-event": ["calendar", "event"],
        "send-email": ["email", "mail", "message"],
        "delete-email": ["email", "mail"],
        "write_file": ["file", "write"],
        "delete_file": ["file", "delete"],
    }

    def _get_domain_keywords(self, tool_name: str) -> list[str]:
        """Return domain keywords for matching against covenant rule descriptions."""
        return self._DOMAIN_KEYWORDS.get(tool_name, [])

    async def _has_prohibiting_covenant(
        self, tool_name: str, tenant_id: str, active_space_id: str,
    ) -> bool:
        """Check if any must_not covenant rule prohibits this tool call.

        must_not rules override EVERYTHING — including explicit instructions.
        This runs BEFORE the fast path to prevent bypass.
        No LLM call — structured data lookup only.
        """
        if not self._state:
            return False
        try:
            rules = await self._state.query_covenant_rules(
                tenant_id,
                context_space_scope=[active_space_id, None],
                active_only=True,
            )
        except Exception as exc:
            logger.warning("Gate: must_not covenant query failed: %s", exc)
            return False

        cap_name = self._get_capability_for_tool(tool_name)
        for rule in rules:
            if rule.rule_type != "must_not":
                continue
            desc_lower = rule.description.lower()
            # Match on capability name
            if cap_name and cap_name.lower() in desc_lower:
                return True
            # Match on tool name
            if tool_name.lower() in desc_lower:
                return True
            # Match on domain keywords
            domain_keywords = self._get_domain_keywords(tool_name)
            if any(kw in desc_lower for kw in domain_keywords):
                return True
        return False

    def _classify_tool_effect(self, tool_name: str, active_space: Any) -> str:
        """Classify a tool call's effect level.

        Returns: "read", "soft_write", "hard_write", or "unknown"
        Kernel tools have hardcoded classifications.
        MCP tools use tool_effects from CapabilityInfo.
        Unknown defaults to "hard_write" (safe default).
        """
        _KERNEL_READS = {"remember", "list_files", "read_file", "request_tool"}
        _KERNEL_WRITES = {"write_file", "delete_file"}

        if tool_name in _KERNEL_READS:
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
        return None

    def _explicit_instruction_matches(
        self,
        tool_name: str,
        tool_input: dict,
        user_message: str,
        messages: list[dict] | None = None,
    ) -> bool:
        """Check if the user's current message directly authorizes this tool call.

        Step 1 fast path — no LLM call. Checks:
        1. Tool-specific instruction signals (imperative verb + domain)
        2. Confirmation signals when context shows a previously blocked action
        """
        msg_lower = user_message.lower()

        # Tool-specific signals
        signals = self._TOOL_SIGNALS.get(tool_name, [])
        if any(signal in msg_lower for signal in signals):
            return True

        # Universal confirmation: user confirms a previously blocked action
        if messages and any(signal in msg_lower for signal in self._CONFIRMATION_SIGNALS):
            # Look for the last assistant message in the conversation
            for msg in reversed(messages[-6:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Content blocks
                        content = " ".join(
                            b.get("text", "") if isinstance(b, dict) else ""
                            for b in content
                        )
                    if isinstance(content, str):
                        content_lower = content.lower()
                        if any(ind in content_lower for ind in self._BLOCKED_CONTEXT_INDICATORS):
                            return True
                    break  # Only check the most recent assistant message

        return False

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

    async def _check_permission_or_covenant(
        self,
        tool_name: str,
        tool_input: dict,
        tenant_id: str,
        active_space_id: str,
    ) -> GateResult:
        """Step 2: Permission override check (fast) then covenant authorization (Haiku).

        Returns GateResult(allowed=True) if authorized, else False.
        """
        # Permission override — fast dict lookup
        cap_name = self._get_capability_for_tool(tool_name)
        if cap_name and self._state:
            try:
                tenant = await self._state.get_tenant_profile(tenant_id)
                if tenant and tenant.permission_overrides:
                    permission = tenant.permission_overrides.get(cap_name)
                    if permission == "always-allow":
                        return GateResult(
                            allowed=True, reason="permission_override",
                            method="always_allow",
                        )
            except Exception as exc:
                logger.warning("Gate: permission check failed: %s", exc)

        # Covenant rules — one Haiku call
        if not self._state:
            return GateResult(allowed=False, reason="no_covenants", method="none")

        try:
            rules = await self._state.query_covenant_rules(
                tenant_id,
                context_space_scope=[active_space_id, None],
                active_only=True,
            )
        except Exception as exc:
            logger.warning("Gate: covenant query failed: %s", exc)
            return GateResult(allowed=False, reason="no_covenants", method="none")

        if not rules:
            return GateResult(allowed=False, reason="no_covenants", method="none")

        action_desc = self._describe_action(tool_name, tool_input)
        rules_text = "\n".join(
            f"- [{r.rule_type}] {r.description} (scope: {r.context_space or 'global'})"
            for r in rules
        )

        try:
            result = await self.complete_simple(
                system_prompt=(
                    "You are checking whether a proposed agent action is authorized by "
                    "any of the user's standing rules (covenants). "
                    "Answer ONLY with: YES, NO, or AMBIGUOUS.\n"
                    "YES = a rule explicitly covers this action.\n"
                    "NO = no rule covers this action.\n"
                    "AMBIGUOUS = a rule might cover this but the scope is unclear."
                ),
                user_content=(
                    f"Proposed action: {action_desc}\n\n"
                    f"Active covenant rules:\n{rules_text}"
                ),
                max_tokens=16,
                prefer_cheap=True,
            )
        except Exception as exc:
            logger.warning("Gate: covenant Haiku call failed: %s", exc)
            return GateResult(allowed=False, reason="no_covenants", method="none")

        answer = result.strip().upper()
        if answer == "YES":
            return GateResult(allowed=True, reason="covenant_authorized", method="haiku_check")

        reason = "covenant_denied" if answer == "NO" else "covenant_ambiguous"
        return GateResult(allowed=False, reason=reason, method="haiku_check")

    async def _gate_tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        user_message: str,
        tenant_id: str,
        active_space_id: str,
        messages: list[dict] | None = None,
    ) -> GateResult:
        """Four-step authorization for write tool calls.

        Step 0: must_not covenant check — prohibitive rules block even explicit instructions
        Step 1: Explicit instruction in current message (fast path, no LLM)
        Step 2: Permission override or covenant authorization (one Haiku call)
        Step 3: Ask user — block and surface proposed action
        """
        # Step 0: must_not covenants override everything (no LLM — structured lookup)
        if await self._has_prohibiting_covenant(tool_name, tenant_id, active_space_id):
            return GateResult(
                allowed=False,
                reason="covenant_prohibited",
                method="must_not_block",
                proposed_action=self._describe_action(tool_name, tool_input),
            )

        # Step 1: Explicit instruction (fast path)
        if self._explicit_instruction_matches(tool_name, tool_input, user_message, messages):
            return GateResult(allowed=True, reason="explicit_instruction", method="fast_path")

        # Step 2: Permission override or covenant
        auth = await self._check_permission_or_covenant(
            tool_name, tool_input, tenant_id, active_space_id,
        )
        if auth.allowed:
            return auth

        # Step 3: Block and ask user — preserve covenant reason if available
        reason = auth.reason if auth.reason != "no_covenants" else "no_authorization"
        return GateResult(
            allowed=False,
            reason=reason,
            method="ask_user",
            proposed_action=self._describe_action(tool_name, tool_input),
        )

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
                    "provider": _PROVIDER,
                    "conversation_id": request.conversation_id,
                    "message_count": len(messages),
                    "tool_count": len(tools),
                    "system_prompt_length": len(request.system_prompt),
                    "trigger": request.trigger,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit reasoning.request: %s", exc)

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

        rr_event = None
        try:
            rr_event = await emit_event(
                self._events,
                EventType.REASONING_RESPONSE,
                request.tenant_id,
                "reasoning_service",
                payload={
                    "model": request.model,
                    "provider": _PROVIDER,
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

        # --- Tool-use loop ---
        iterations = 0
        while (
            response.stop_reason == "tool_use"
            and iterations < self.MAX_TOOL_ITERATIONS
        ):
            iterations += 1
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info(
                    "TOOL_LOOP iter=%d tool=%s kernel=%s",
                    iterations, block.name, block.name in self._KERNEL_TOOLS,
                )

                # Dispatch Gate: classify and check write tools before execution
                tool_effect = self._classify_tool_effect(block.name, request.active_space)
                if tool_effect in ("soft_write", "hard_write", "unknown"):
                    gate_result = await self._gate_tool_call(
                        block.name, block.input or {}, tool_effect,
                        request.input_text, request.tenant_id,
                        request.active_space_id,
                        messages=request.messages,
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
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": (
                                f"[SYSTEM] Action blocked by dispatch gate. "
                                f"Proposed: {gate_result.proposed_action}. "
                                f"No explicit instruction or standing rule authorizes this. "
                                f"Ask the user for permission before proceeding. "
                                f"If they confirm, you may offer to create a standing rule."
                            ),
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
                            "tool_input": block.input,
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
                        "tool_input": block.input,
                    },
                )

                t_tool = time.monotonic()
                # Kernel tool routing: remember + file tools handled internally
                if block.name in self._KERNEL_TOOLS:
                    logger.info(
                        "KERNEL_TOOL name=%s space=%s",
                        block.name, request.active_space_id,
                    )
                    tool_args = block.input or {}
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
                    elif block.name == "request_tool":
                        result = await self._handle_request_tool(
                            request.tenant_id,
                            request.active_space_id,
                            tool_args.get("capability_name", "unknown"),
                            tool_args.get("description", ""),
                        )
                    else:
                        result = f"Kernel tool '{block.name}' not handled."
                else:
                    result = await self._mcp.call_tool(block.name, block.input)
                tool_duration_ms = int((time.monotonic() - t_tool) * 1000)

                is_error = result.startswith("Tool error:") or result.startswith(
                    "Calendar tool error:"
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
                        "provider": _PROVIDER,
                        "conversation_id": request.conversation_id,
                        "message_count": len(messages),
                        "tool_count": len(tools),
                        "system_prompt_length": len(request.system_prompt),
                        "trigger": "tool_continuation",
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit reasoning.request: %s", exc)

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

            rr_event = None
            try:
                rr_event = await emit_event(
                    self._events,
                    EventType.REASONING_RESPONSE,
                    request.tenant_id,
                    "reasoning_service",
                    payload={
                        "model": request.model,
                        "provider": _PROVIDER,
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

        return ReasoningResult(
            text=response_text,
            model=request.model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimated_cost,
            duration_ms=total_duration_ms,
            tool_iterations=iterations,
        )
