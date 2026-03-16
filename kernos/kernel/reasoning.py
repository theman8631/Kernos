"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
import hashlib
import json
import logging
import time
import uuid
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
    max_tokens: int = 8192
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
        self._approval_tokens: dict[str, ApprovalToken] = {}  # In-memory token store

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
            if output_schema:
                return "{}"
            # Plain-text call: return whatever was generated (partial is better than "{}")
        if response.stop_reason == "refusal":
            logger.warning("complete_simple: response refused by model")
            return "{}"
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "".join(text_parts)

    # Kernel tools: intercepted before MCP, never passed through to external servers
    _KERNEL_TOOLS = {"remember", "write_file", "read_file", "list_files", "delete_file", "request_tool"}

    # ---------------------------------------------------------------------------
    # Dispatch Gate (3D-HOTFIX)
    # ---------------------------------------------------------------------------

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
        logger.info("GATE_MODEL: max_tokens=256, has_schema=False, rules=%d", rules_count)
        try:
            raw = await self.complete_simple(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=256,
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

        # Estimate context size: rough token count of system prompt + messages.
        # 1 token ≈ 4 chars is a reasonable approximation for English prose.
        _ctx_chars = len(request.system_prompt) + sum(
            len(m.get("content", "") if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", "")))
            for m in messages
        )
        _ctx_tokens_est = _ctx_chars // 4
        logger.info(
            "REASON_START: tool_count=%d max_tokens=%d msg_count=%d ctx_tokens_est=%d",
            len(tools), request.max_tokens, len(messages), _ctx_tokens_est,
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

                # Dispatch Gate: classify and check write tools before execution
                tool_effect = self._classify_tool_effect(block.name, request.active_space)
                if tool_effect in ("soft_write", "hard_write", "unknown"):
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
                        token = self._issue_approval_token(block.name, tool_input)
                        if gate_result.reason == "covenant_conflict":
                            system_msg = (
                                f"[SYSTEM] Action paused — conflict with standing rule. "
                                f"Proposed: {gate_result.proposed_action}. "
                                f"Conflicting rule: {gate_result.conflicting_rule}. "
                                f"The user may be knowingly overriding this rule. "
                                f"Ask for clarification. Offer three options: "
                                f"(1) respect the rule, (2) override just this time with "
                                f"_approval_token: '{token.token_id}', "
                                f"(3) update or remove the rule permanently."
                            )
                        else:
                            system_msg = (
                                f"[SYSTEM] Action blocked — no authorization found. "
                                f"Proposed: {gate_result.proposed_action}. "
                                f"The user's recent messages do not request this action "
                                f"and no covenant rule covers it. "
                                f"Ask the user if they'd like you to proceed. "
                                f"If they confirm, re-submit with "
                                f"_approval_token: '{token.token_id}' in the tool input. "
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
                    result = await self._mcp.call_tool(block.name, tool_input)
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

        # Hallucination check: agent claims tool use in text but no tool was actually called.
        # This fires when stop_reason=end_turn AND iterations=0 AND response contains
        # tool-claiming language. Most common cause: max_tokens truncation cut off tool_use
        # blocks, or the model chose end_turn without generating tool_use content.
        if iterations == 0 and response.stop_reason == "end_turn":
            _TOOL_CLAIM_PHRASES = (
                "used write_file", "used delete_file", "used read_file",
                "used list_files", "used create-event", "used send-email",
                "i created", "i deleted", "i wrote", "i've created", "i've deleted",
                "i've written", "file created", "file deleted", "file written",
                "done —", "done.", "✅",
            )
            rt_lower = response_text.lower()
            if any(phrase in rt_lower for phrase in _TOOL_CLAIM_PHRASES):
                logger.warning(
                    "HALLUCINATION_CHECK: Agent claims tool use but iterations=0 "
                    "(stop=%s, tool_count=%d). Response may be fabricated. "
                    "Response: %s",
                    response.stop_reason, len(tools), response_text[:200],
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
