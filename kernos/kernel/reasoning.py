"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
from kernos.utils import utc_now
import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event, estimate_cost
from kernos.kernel.exceptions import (
    LLMChainExhausted,
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

# Tool result budgeting — Stage 1 of Tool Execution Mediation.
# MCP results exceeding this threshold are persisted to the space file store
# and replaced with a bounded preview + file reference.
TOOL_RESULT_CHAR_BUDGET = 4000  # ~1000 tokens


# Tool schemas extracted to kernos/kernel/tools/schemas.py
from kernos.kernel.tools import (
    REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL,
    MANAGE_CAPABILITIES_TOOL, READ_SOURCE_TOOL,
    READ_SOUL_TOOL, UPDATE_SOUL_TOOL, SOUL_UPDATABLE_FIELDS,
    read_doc as _read_doc, read_source as _read_source,
    SOUL_UPDATABLE_FIELDS as _SOUL_UPDATABLE_FIELDS,
)


# ---------------------------------------------------------------------------
# KERNOS-native content types — no provider types leak past this module
# ---------------------------------------------------------------------------


# Provider types re-exported for backward compatibility
from kernos.providers.base import ChainConfig, ChainEntry, ContentBlock, Provider, ProviderResponse


from kernos.providers.anthropic_provider import AnthropicProvider  # re-export
from kernos.providers.codex_provider import OpenAICodexProvider  # re-export
from kernos.kernel.gate import DispatchGate, GateResult, ApprovalToken  # re-export


# OpenAICodexProvider extracted to kernos/providers/codex_provider.py
# ---------------------------------------------------------------------------
# Request / Result types
# ---------------------------------------------------------------------------


@dataclass
class ReasoningRequest:
    """Everything the ReasoningService needs to run a reasoning turn."""

    instance_id: str
    conversation_id: str
    system_prompt: str
    messages: list[dict]
    tools: list[dict]
    model: str
    trigger: str
    max_tokens: int = 64000  # Sonnet/Opus output limit — let the model decide when to stop
    active_space_id: str = ""  # For kernel tool routing (e.g., remember)
    member_id: str = ""        # Current member — for per-member tool writes
    input_text: str = ""       # Current user message — used by dispatch gate
    active_space: Any = None   # ContextSpace | None — for gate tool effect classification
    user_timezone: str = ""    # IANA timezone from soul — for scheduler extraction
    is_reactive: bool = True   # True when responding to a user message; False for scheduler/background
    system_prompt_static: str = ""   # Cacheable prefix (RULES + ACTIONS)
    system_prompt_dynamic: str = ""  # Fresh per turn (NOW + STATE + RESULTS + MEMORY)
    trace: Any = None  # TurnEventCollector — for runtime trace instrumentation


# GateResult and ApprovalToken extracted to kernos/kernel/gate.py (re-exported above)

@dataclass
class PendingAction:
    """A tool call blocked by the dispatch gate, awaiting user confirmation.

    Stored on the ReasoningService keyed by instance_id. The handler executes
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


def _build_chains_from_legacy(
    provider: Provider,
    fallback_providers: list[Provider] | None = None,
    fallback_provider: Provider | None = None,
) -> ChainConfig:
    """Synthesize a ChainConfig from old-style provider + fallback args.

    Used by tests and legacy call sites that construct ReasoningService with
    positional provider arguments instead of the new chains kwarg.
    """
    fallbacks = list(fallback_providers or [])
    if fallback_provider and fallback_provider not in fallbacks:
        fallbacks.append(fallback_provider)

    all_providers = [provider] + fallbacks
    return {
        "primary": [ChainEntry(provider=p, model=getattr(p, "main_model", "unknown")) for p in all_providers],
        "simple": [ChainEntry(provider=p, model=getattr(p, "simple_model", _SIMPLE_MODEL)) for p in all_providers],
        "cheap": [ChainEntry(provider=p, model=getattr(p, "cheap_model", _CHEAP_MODEL)) for p in all_providers],
    }


class ReasoningService:
    """Owns the full tool-use reasoning loop. Provider-agnostic.

    Emits reasoning.request, reasoning.response, tool.called, tool.result events.
    Logs tool calls and results to the audit store.
    Raises ReasoningError subtypes on provider failure — does NOT catch them.
    """

    MAX_TOOL_ITERATIONS = 10
    MAX_TOOL_ITERATIONS_PLAN = 25  # Self-directed plan steps need more room for research

    def __init__(
        self,
        provider: Provider | None = None,
        events: EventStream | None = None,
        mcp: Any = None,    # MCPClientManager — Any avoids circular import with capability layer
        audit: Any = None,  # AuditStore
        fallback_providers: list[Provider] | None = None,
        # Legacy single fallback — converted to list internally
        fallback_provider: Provider | None = None,
        *,
        chains: ChainConfig | None = None,
    ) -> None:
        if chains is not None:
            self._chains = chains
            self._provider = chains["primary"][0].provider
        else:
            assert provider is not None, "Either provider or chains must be supplied"
            self._provider = provider
            self._chains = _build_chains_from_legacy(provider, fallback_providers, fallback_provider)
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
        self._canvas = None            # Set by handler after construction (CanvasService)
        self._gate: DispatchGate | None = None  # Created lazily after registry/state are set
        self._pending_actions: dict[str, list[PendingAction]] = {}  # instance_id → list
        self._conflict_raised_this_turn: bool = False  # Set when gate blocks; cleared at turn start
        self._tools_changed: bool = False  # Set by manage_capabilities; handler checks post-reasoning
        # Lazy tool loading: tracks which MCP tools have been loaded per-space session
        self._loaded_tools: dict[str, set[str]] = {}  # space_id → set of tool names
        # Turn-level tool call trace — accumulated during reasoning, read+cleared by handler
        self._turn_tool_trace: list[dict] = []
        # Hybrid token counting: real input_tokens from last principal reasoning call per-instance
        self._last_real_input_tokens: dict[str, int] = {}  # instance_id → tokens

    @staticmethod
    def _trace(request: "ReasoningRequest", level: str, source: str, event: str, detail: str, **kw: Any) -> None:
        """Record a trace event if collector is available."""
        if request and getattr(request, 'trace', None):
            request.trace.record(level, source, event, detail, **kw)

    def _get_gate(self) -> DispatchGate:
        """Lazy gate creation — registry/state set after construction."""
        if not hasattr(self, '_gate') or self._gate is None:
            self._gate = DispatchGate(
                reasoning_service=self,
                registry=getattr(self, '_registry', None),
                state=getattr(self, '_state', None),
                events=getattr(self, '_events', None),
                mcp=getattr(self, '_mcp', None),
            )
        return self._gate

    def cleanup_expired_authorizations(self, instance_id: str) -> None:
        """Remove expired PendingActions and used/expired ApprovalTokens."""
        now = datetime.now(timezone.utc)

        if instance_id in self._pending_actions:
            self._pending_actions[instance_id] = [
                a for a in self._pending_actions[instance_id]
                if now < a.expires_at
            ]
            if not self._pending_actions[instance_id]:
                del self._pending_actions[instance_id]

        self._get_gate().cleanup_expired_tokens()

    @staticmethod
    def _is_stub_schema(tool_entry: dict) -> bool:
        """Check if a tool entry has a stub schema (open input, no properties)."""
        schema = tool_entry.get("input_schema", {})
        return schema.get("additionalProperties") is True and not schema.get("properties")

    def set_retrieval(self, retrieval: Any) -> None:
        """Wire up the retrieval service for kernel tool routing."""
        self._retrieval = retrieval

    def set_files(self, files: Any) -> None:
        """Wire up the file service for kernel tool routing."""
        self._files = files

    def set_registry(self, registry: Any) -> None:
        """Wire up the capability registry for request_tool routing."""
        self._registry = registry

    def set_workspace(self, workspace: Any) -> None:
        """Wire up the workspace manager for manage_workspace/register_tool."""
        self._workspace = workspace

    def set_state(self, state: Any) -> None:
        """Wire up the state store for request_tool activation."""
        self._state = state

    def set_channel_registry(self, registry: Any) -> None:
        """Wire up the channel registry for send_to_channel."""
        self._channel_registry = registry

    def set_trigger_store(self, store: Any) -> None:
        """Wire up the trigger store for manage_schedule."""
        self._trigger_store = store

    def set_handler(self, handler: Any) -> None:
        """Wire up the handler (implements HandlerProtocol)."""
        self._handler = handler

    def set_canvas(self, canvas: Any) -> None:
        """Wire up the CanvasService for canvas_* / page_* tool routing."""
        self._canvas = canvas

    # --- Public state accessors (replace private attribute access from handler) ---

    def get_pending_actions(self, instance_id: str) -> list[PendingAction] | None:
        """Return a copy of pending actions for an instance, or None."""
        actions = self._pending_actions.get(instance_id)
        if actions is None:
            return None
        return list(actions)  # copy — caller cannot mutate internal list

    def clear_pending_actions(self, instance_id: str) -> None:
        """Remove all pending actions for an instance."""
        self._pending_actions.pop(instance_id, None)

    def get_conflict_raised(self) -> bool:
        """Whether a gate conflict was raised this turn."""
        return self._conflict_raised_this_turn

    def reset_conflict_raised(self) -> None:
        """Reset the per-turn conflict flag and gate denial counters."""
        self._conflict_raised_this_turn = False
        if hasattr(self, '_gate') and self._gate:
            self._gate.reset_denial_counts()

    def get_tools_changed(self) -> bool:
        """Whether manage_capabilities changed tool state this turn."""
        return self._tools_changed

    def reset_tools_changed(self) -> None:
        """Reset the tools-changed flag."""
        self._tools_changed = False

    @property
    def main_model(self) -> str:
        """The primary model name from the provider."""
        entries = self._chains.get("primary", [])
        return entries[0].model if entries else "unknown"

    async def _call_chain(
        self,
        chain_name: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        request_model: str | None = None,
        request: "ReasoningRequest | None" = None,
    ) -> ProviderResponse:
        """Try each entry in the named chain until one succeeds.

        For the "primary" chain, the first entry uses request_model (from
        ReasoningRequest) rather than the chain's configured model — this
        preserves the current handler → reasoning model selection.

        Catches only ReasoningProviderError | ReasoningConnectionError to
        match existing behavior and avoid masking programming errors.
        """
        entries = self._chains.get(chain_name, self._chains.get("primary", []))
        last_exc: Exception | None = None
        # LLM-SETUP-AND-FALLBACK: accumulate per-entry failure detail so the
        # LLMChainExhausted exception can carry it for the pre-rendered
        # failure message and diagnostic tools.
        attempts: list[tuple[str, str, str]] = []

        for i, entry in enumerate(entries):
            model = request_model if (i == 0 and request_model) else entry.model
            pname = getattr(entry.provider, "provider_name", "unknown")

            # Thread trace to provider for internal event capture
            if hasattr(entry.provider, "_trace"):
                entry.provider._trace = getattr(request, "trace", None) if request else None

            try:
                response = await entry.provider.complete(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                )
                if i > 0 and request:
                    # Partial fallback succeeded — silent per the
                    # LLM-SETUP-AND-FALLBACK contract. Log a FALLBACK_USED
                    # diagnostic event only; never surface to the agent or
                    # user, never fire a whisper.
                    self._trace(request, "info", "reasoning", "FALLBACK_USED",
                        f"chain={chain_name} via {pname}/{model} (skipped {i} entries)")
                    logger.info("CHAIN[%s]: success via %s/%s", chain_name, pname, model)
                return response
            except (ReasoningProviderError, ReasoningConnectionError) as exc:
                if request:
                    self._trace(request, "warning", "reasoning", "CHAIN_FALLBACK",
                        f"chain={chain_name} {pname}/{model} failed: {str(exc)[:150]}")
                logger.warning("CHAIN[%s]: %s/%s failed: %s", chain_name, pname, model, exc)
                last_exc = exc
                attempts.append((pname, model, str(exc)))
                continue

        # All entries exhausted — raise the specific chain-exhaustion
        # exception the handler catches to deliver a pre-rendered failure
        # message (instead of an LLM reply) for this turn.
        if request:
            self._trace(request, "error", "reasoning", "CHAIN_EXHAUSTED",
                f"chain={chain_name} all {len(entries)} entries exhausted")
        logger.error("CHAIN[%s]: all %d providers failed", chain_name, len(entries))
        raise LLMChainExhausted(chain_name=chain_name, attempts=attempts)

    def get_loaded_tools(self, space_id: str) -> set[str]:
        """Get the set of MCP tool names currently loaded for a space."""
        return self._loaded_tools.get(space_id, set())

    def load_tool(self, space_id: str, tool_name: str) -> None:
        """Add a tool to the loaded set for a space."""
        if space_id not in self._loaded_tools:
            self._loaded_tools[space_id] = set()
        self._loaded_tools[space_id].add(tool_name)

    def get_last_real_input_tokens(self, instance_id: str) -> int:
        """Return the real input_tokens from the last principal reasoning call, or 0."""
        return self._last_real_input_tokens.get(instance_id, 0)

    def drain_tool_trace(self) -> list[dict]:
        """Return and clear the accumulated tool call trace for the current turn."""
        trace = self._turn_tool_trace
        self._turn_tool_trace = []
        return trace

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
        chain: str | None = None,
    ) -> str:
        """Single stateless completion. No tools, no history, no task events.

        Used by kernel infrastructure (extraction, consolidation) not by agents.
        Returns raw text response. prefer_cheap uses Haiku-class model for cost efficiency.

        chain: explicit chain name override ("primary", "simple", "cheap").
        When omitted, prefer_cheap selects "cheap" or "simple".

        When output_schema is provided, uses Anthropic's native structured outputs
        (constrained decoding). Schema compliance is guaranteed by the API — no
        json.loads() retry logic needed. Returns "{}" on truncation or refusal.
        """
        chain_name = chain or ("cheap" if prefer_cheap else "simple")
        entries = self._chains.get(chain_name, self._chains.get("simple", []))
        messages = [{"role": "user", "content": user_content}]

        # Walk the chain until one provider succeeds
        last_exc: Exception | None = None
        response = None
        for entry in entries:
            pname = getattr(entry.provider, "provider_name", type(entry.provider).__name__)
            try:
                response = await entry.provider.complete(
                    model=entry.model,
                    system=system_prompt,
                    messages=messages,
                    tools=[],
                    max_tokens=max_tokens,
                    output_schema=output_schema,
                )
                break  # Success
            except Exception as exc:
                logger.warning("complete_simple[%s]: %s/%s failed: %s", chain_name, pname, entry.model, exc)
                last_exc = exc
                continue

        if response is None:
            raise last_exc or RuntimeError(f"complete_simple: all providers in chain '{chain_name}' failed")

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
    _KERNEL_TOOLS = {"remember", "remember_details", "write_file", "read_file", "list_files", "delete_file", "dismiss_whisper", "read_source", "read_doc", "read_soul", "update_soul", "manage_covenants", "manage_capabilities", "manage_channels", "send_to_channel", "manage_schedule", "inspect_state", "request_tool", "execute_code", "manage_workspace", "register_tool", "manage_plan", "read_runtime_trace", "diagnose_issue", "propose_fix", "submit_spec", "manage_members", "send_relational_message", "resolve_relational_message", "set_chain_model", "diagnose_llm_chain", "diagnose_messenger", "canvas_list", "canvas_create", "page_read", "page_write", "page_list", "page_search"}

    # ---------------------------------------------------------------------------
    # Dispatch Gate (3D-HOTFIX)
    # ---------------------------------------------------------------------------

    # Gate methods extracted to kernos/kernel/gate.py — accessed via self._get_gate()
    # Delegation methods for backward compatibility (tests call these directly)
    def _classify_tool_effect(self, tool_name: str, active_space: Any, tool_input: dict | None = None) -> str:
        return self._get_gate().classify_tool_effect(tool_name, active_space, tool_input)

    def _describe_action(self, tool_name: str, tool_input: dict) -> str:
        return self._get_gate()._describe_action(tool_name, tool_input)

    def _get_capability_for_tool(self, tool_name: str) -> str | None:
        return self._get_gate()._get_capability_for_tool(tool_name)

    def _get_tool_description(self, tool_name: str) -> str:
        return self._get_gate()._get_tool_description(tool_name)

    async def _gate_tool_call(self, *args, **kwargs) -> GateResult:
        return await self._get_gate().evaluate(*args, **kwargs)

    async def _evaluate_gate(self, *args, **kwargs) -> GateResult:
        return await self._get_gate()._evaluate_model(*args, **kwargs)

    def _issue_approval_token(self, tool_name: str, tool_input: dict) -> ApprovalToken:
        return self._get_gate().issue_approval_token(tool_name, tool_input)

    def _validate_approval_token(self, token_id: str, tool_name: str, tool_input: dict) -> bool:
        return self._get_gate().validate_approval_token(token_id, tool_name, tool_input)

    @property
    def _approval_tokens(self) -> dict:
        """Backward compat — tokens now live on the gate."""
        return self._get_gate()._approval_tokens

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
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                        tool_input.get("content", ""),
                        tool_input.get("description", ""),
                        target_space_id=tool_input.get("target_space_id"),
                    )
                return "File system is not available."
            elif tool_name == "read_file":
                if self._files:
                    return await self._files.read_file(
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "list_files":
                if self._files:
                    return await self._files.list_files(
                        request.instance_id,
                        request.active_space_id,
                    )
                return "File system is not available."
            elif tool_name == "delete_file":
                if self._files:
                    return await self._files.delete_file(
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "execute_code":
                import json as _json
                from kernos.kernel.code_exec import execute_code as _exec_code
                data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                result = await _exec_code(
                    instance_id=request.instance_id,
                    space_id=request.active_space_id,
                    code=tool_input.get("code", ""),
                    timeout_seconds=tool_input.get("timeout_seconds", 30),
                    write_file_name=tool_input.get("write_file"),
                    data_dir=data_dir,
                )
                return _json.dumps(result)
            elif tool_name == "manage_workspace":
                if self._workspace:
                    action = tool_input.get("action", "list")
                    if action == "list":
                        return await self._workspace.list_artifacts(request.instance_id, request.active_space_id)
                    elif action == "add":
                        msg, _ = await self._workspace.add_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact", {}))
                        return msg
                    elif action == "update":
                        return await self._workspace.update_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact_id", ""), tool_input.get("artifact", {}))
                    elif action == "archive":
                        return await self._workspace.archive_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact_id", ""))
                    return f"Unknown action: {action}"
                return "Workspace manager is not available."
            elif tool_name == "register_tool":
                if self._workspace:
                    _desc_file = tool_input.get("descriptor_file", "") or tool_input
                    return await self._workspace.register_tool(request.instance_id, request.active_space_id, _desc_file)
                return "Workspace manager is not available."
            elif tool_name == "manage_plan":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_manage_plan(
                        request.instance_id, request.active_space_id, tool_input)
                return "Self-directed execution is not available."
            elif tool_name == "read_runtime_trace":
                if hasattr(self, '_handler') and self._handler:
                    _turns = tool_input.get("turns", 10)
                    _filter = tool_input.get("filter", None)
                    _turn_id = tool_input.get("turn_id", None)
                    events = await self._handler._runtime_trace.read(
                        request.instance_id, turns=_turns,
                        filter_level=_filter, turn_id=_turn_id)
                    if not events:
                        return "No trace events found."
                    lines = []
                    for e in events:
                        lines.append(
                            f"[{e.get('timestamp', '?')[:19]}] {e.get('level', '?').upper()} "
                            f"{e.get('source', '?')}:{e.get('event', '?')} — {e.get('detail', '')[:200]}"
                        )
                    return f"Runtime trace ({len(events)} events):\n" + "\n".join(lines)
                return "Runtime trace is not available."
            elif tool_name in ("diagnose_issue", "propose_fix", "submit_spec"):
                from kernos.kernel.diagnostics import handle_diagnose_issue, handle_propose_fix, handle_submit_spec
                _rt = getattr(self._handler, '_runtime_trace', None) if self._handler else None
                if tool_name == "diagnose_issue":
                    return await handle_diagnose_issue(
                        request.instance_id, request.active_space_id, tool_input, _rt, self)
                elif tool_name == "propose_fix":
                    return await handle_propose_fix(request.instance_id, tool_input, _rt)
                else:
                    return await handle_submit_spec(request.instance_id, tool_input, self._handler)
            elif tool_name == "manage_members":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_manage_members(request.instance_id, tool_input, requesting_member_id=request.member_id)
                return "Member management is not available."
            elif tool_name == "send_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_send_relational_message(
                        request.instance_id, tool_input,
                        origin_member_id=request.member_id,
                    )
                return "Relational messaging is not available."
            elif tool_name == "resolve_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_resolve_relational_message(
                        request.instance_id, tool_input,
                        requesting_member_id=request.member_id,
                    )
                return "Relational messaging is not available."
            elif tool_name == "remember":
                if self._retrieval:
                    _idb = (
                        getattr(self._handler, '_instance_db', None)
                        if hasattr(self, '_handler') and self._handler
                        else None
                    )
                    return await self._retrieval.search(
                        request.instance_id,
                        tool_input.get("query", ""),
                        request.active_space_id,
                        requesting_member_id=getattr(request, "member_id", ""),
                        instance_db=_idb,
                    )
                return "Memory search is not available."
            elif tool_name == "dismiss_whisper":
                return await self._handle_dismiss_whisper(
                    request.instance_id,
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
                # Per-member: return member profile (the real identity state)
                member_id = getattr(request, "member_id", "")
                if member_id and hasattr(self, "_handler") and self._handler:
                    idb = getattr(self._handler, "_instance_db", None)
                    if idb:
                        profile = await idb.get_member_profile(member_id)
                        if profile:
                            return json.dumps(profile, indent=2, default=str)
                # Fallback: instance soul
                if self._state:
                    soul = await self._state.get_soul(request.instance_id)
                    if soul:
                        from dataclasses import asdict
                        return json.dumps(asdict(soul), indent=2)
                    return "No soul found for this instance."
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
                    # Per-member soul fields → write to member_profiles
                    _MEMBER_SOUL_FIELDS = {"agent_name", "emoji", "personality_notes", "communication_style"}
                    member_id = getattr(request, "member_id", "") if hasattr(request, "member_id") else ""
                    if field in _MEMBER_SOUL_FIELDS and member_id and hasattr(self, "_handler") and self._handler:
                        idb = getattr(self._handler, "_instance_db", None)
                        if idb:
                            await idb.upsert_member_profile(member_id, {field: value})
                            return f"Updated {field} to: {value}"
                    # Legacy fallback: write to instance soul
                    soul = await self._state.get_soul(request.instance_id)
                    if not soul:
                        return "No soul found for this instance."
                    setattr(soul, field, value)
                    await self._state.save_soul(soul, source="update_soul", trigger=f"{field}={value}")
                    return f"Updated {field} to: {value}"
                return "State store is not available."
            elif tool_name == "manage_covenants":
                from kernos.kernel.covenant_manager import handle_manage_covenants
                cov_action = tool_input.get("action", "list")
                cov_result = await handle_manage_covenants(
                    self._state,
                    request.instance_id,
                    action=cov_action,
                    rule_id=tool_input.get("rule_id", ""),
                    new_description=tool_input.get("new_description", ""),
                    show_all=tool_input.get("show_all", False),
                )
                if cov_action == "update" and "Updated" in cov_result:
                    import asyncio
                    from kernos.kernel.covenant_manager import validate_covenant_set
                    id_match = re.search(r"new ID: (rule_\w+)", cov_result)
                    new_id = id_match.group(1) if id_match else ""
                    if new_id:
                        asyncio.create_task(
                            validate_covenant_set(
                                state=self._state,
                                events=self._events,
                                reasoning_service=self,
                                instance_id=request.instance_id,
                                new_rule_id=new_id,
                            )
                        )
                return cov_result
            elif tool_name == "manage_capabilities":
                return await self._handle_manage_capabilities(
                    request.instance_id,
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
            elif tool_name == "send_to_channel":
                from kernos.kernel.channels import resolve_channel_alias
                from kernos.kernel.scheduler import resolve_owner_member_id
                channel_input = tool_input.get("channel", "")
                message_text = tool_input.get("message", "")
                if not channel_input or not message_text:
                    return "Error: both 'channel' and 'message' are required."
                resolved = resolve_channel_alias(channel_input)
                if not self._channel_registry:
                    return "Channel registry is not available."
                ch_info = self._channel_registry.get(resolved)
                if not ch_info:
                    available = [c.name for c in self._channel_registry.get_connected()]
                    return (
                        f"Channel '{resolved}' (from '{channel_input}') is not registered. "
                        f"Available channels: {', '.join(available) or 'none'}"
                    )
                if ch_info.status != "connected":
                    return f"Channel '{resolved}' exists but is not connected (status: {ch_info.status})."
                if not ch_info.can_send_outbound:
                    return f"Channel '{resolved}' is connected but cannot send outbound messages."
                if not self._handler:
                    return "Handler not available for outbound delivery."
                try:
                    member_id = resolve_owner_member_id(request.instance_id)
                    await self._handler.send_outbound(
                        request.instance_id, member_id, resolved, message_text,
                    )
                    logger.info(
                        "CROSS_CHANNEL_SEND: channel=%s resolved_from=%s len=%d",
                        resolved, channel_input, len(message_text),
                    )
                    return f"Message sent to {ch_info.display_name}."
                except Exception as exc:
                    return f"Failed to send to {resolved}: {exc}"
            elif tool_name == "manage_schedule":
                from kernos.kernel.scheduler import handle_manage_schedule
                if self._trigger_store:
                    return await handle_manage_schedule(
                        self._trigger_store,
                        request.instance_id,
                        member_id=request.active_space_id,
                        space_id=request.active_space_id,
                        action=tool_input.get("action", "list"),
                        trigger_id=tool_input.get("trigger_id", ""),
                        description=tool_input.get("description", ""),
                        reasoning_service=self,
                        conversation_id=request.conversation_id,
                        user_timezone=request.user_timezone,
                    )
                return "Scheduler is not available."
            elif tool_name == "request_tool":
                return await self._handle_request_tool(
                    request.instance_id,
                    request.active_space_id,
                    tool_input.get("capability_name", "unknown"),
                    tool_input.get("description", ""),
                )
            elif tool_name in ("canvas_list", "canvas_create", "page_read",
                                "page_write", "page_list", "page_search"):
                return await self._handle_canvas_tool(tool_name, tool_input, request)
            else:
                return f"Kernel tool '{tool_name}' not handled."
        else:
            return await self._mcp.call_tool(tool_name, tool_input)

    def _is_concurrent_safe(self, tool_name: str) -> bool:
        """A tool is concurrent-safe ONLY if explicitly classified as 'read'.

        Unknown, soft_write, hard_write all stay sequential.
        Conservative: if classification fails, return False.
        """
        try:
            effect = self._get_gate().classify_tool_effect(tool_name, None, None)
            return effect == "read"
        except Exception:
            return False

    async def _execute_single_tool(
        self,
        block: ContentBlock,
        tool_input: dict,
        request: ReasoningRequest,
        tools: list[dict],
        gate_cache: dict,
        rr_event: Any,
        iterations: int,
        agent_reasoning: str,
    ) -> dict:
        """Execute a single tool call: gate -> execute -> budget -> return tool_result dict.

        Returns a dict with keys: type, tool_use_id, content.
        Side effects: may modify gate_cache, _pending_actions, _conflict_raised_this_turn.
        """
        approval_token_id = tool_input.pop("_approval_token", None)

        logger.info(
            "AGENT_ACTION: tool=%s input=%s",
            block.name, json.dumps(tool_input)[:200],
        )

        # Dispatch Gate: classify and check write tools before execution
        tool_effect = self._get_gate().classify_tool_effect(
            block.name, request.active_space, tool_input)
        if tool_effect in ("soft_write", "hard_write", "unknown"):
            if block.name in gate_cache and gate_cache[block.name].allowed:
                gate_result = gate_cache.pop(block.name)
                logger.info(
                    "GATE_CACHED: tool=%s (approved on stub call)", block.name,
                )
            else:
                logger.info(
                    "GATE_INPUT: tool=%s effect=%s reasoning=%s",
                    block.name, tool_effect, agent_reasoning[:80],
                )
                gate_result = await self._get_gate().evaluate(
                    block.name, tool_input, tool_effect,
                    request.input_text, request.instance_id,
                    request.active_space_id,
                    messages=request.messages,
                    approval_token_id=approval_token_id,
                    agent_reasoning=agent_reasoning,
                    is_reactive=request.is_reactive,
                )

            try:
                await emit_event(
                    self._events,
                    EventType.DISPATCH_GATE,
                    request.instance_id,
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

            # EVENT-STREAM-TO-SQLITE: unified-timeline emission. Coexists
            # with the older emit_event above during the transition.
            try:
                from kernos.kernel import event_stream
                _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
                await event_stream.emit(
                    request.instance_id, "gate.verdict",
                    {
                        "tool": block.name,
                        "effect": tool_effect,
                        "verdict": gate_result.reason,
                        "allowed": gate_result.allowed,
                        "method": gate_result.method,
                    },
                    member_id=request.member_id or None,
                    space_id=request.active_space_id or None,
                    correlation_id=_turn_id,
                )
            except Exception as exc:
                logger.warning("Failed to emit gate.verdict: %s", exc)

            self._trace(request, "info" if gate_result.allowed else "warning",
                "gate", "GATE",
                f"tool={block.name} effect={tool_effect} allowed={gate_result.allowed} "
                f"reason={gate_result.reason} method={gate_result.method}",
                phase="reason")
            logger.info(
                "GATE: tool=%s effect=%s allowed=%s reason=%s method=%s",
                block.name, tool_effect, gate_result.allowed,
                gate_result.reason, gate_result.method,
            )

            if not gate_result.allowed:
                self._get_gate().issue_approval_token(block.name, tool_input)
                instance_id = request.instance_id
                if instance_id not in self._pending_actions:
                    self._pending_actions[instance_id] = []
                pending_idx = len(self._pending_actions[instance_id])
                self._pending_actions[instance_id].append(PendingAction(
                    tool_name=block.name,
                    tool_input=dict(tool_input),
                    proposed_action=gate_result.proposed_action,
                    conflicting_rule=gate_result.conflicting_rule,
                    gate_reason=gate_result.reason,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                ))
                self._conflict_raised_this_turn = True
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
                elif gate_result.reason == "clarify":
                    system_msg = (
                        f"[SYSTEM] Action paused — the request is ambiguous. "
                        f"Proposed: {gate_result.proposed_action}. "
                        f"Pending action index: {pending_idx}. "
                        f"Ask the user to clarify what they meant. "
                        f"Once clear, include [CONFIRM:{pending_idx}] in your response."
                    )
                else:
                    system_msg = (
                        f"[SYSTEM] Action paused — confirming intent. "
                        f"Proposed: {gate_result.proposed_action}. "
                        f"Pending action index: {pending_idx}. "
                        f"Ask the user if they want to proceed. If they confirm, "
                        f"include [CONFIRM:{pending_idx}] in your response."
                    )
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": system_msg,
                }

        # Emit tool.called
        try:
            await emit_event(
                self._events,
                EventType.TOOL_CALLED,
                request.instance_id,
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

        # EVENT-STREAM-TO-SQLITE: unified-timeline emission.
        try:
            from kernos.kernel import event_stream
            _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
            await event_stream.emit(
                request.instance_id, "tool.called",
                {
                    "name": block.name,
                    "args_keys": sorted((tool_input or {}).keys()),
                },
                member_id=request.member_id or None,
                space_id=request.active_space_id or None,
                correlation_id=_turn_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit tool.called (unified): %s", exc)

        await self._audit.log(
            request.instance_id,
            {
                "type": "tool_call",
                "timestamp": utc_now(),
                "instance_id": request.instance_id,
                "conversation_id": request.conversation_id,
                "tool_name": block.name,
                "tool_input": tool_input,
            },
        )

        t_tool = time.monotonic()
        _is_mcp_tool = False
        result = ""

        if block.name in self._KERNEL_TOOLS:
            logger.info(
                "KERNEL_TOOL name=%s space=%s",
                block.name, request.active_space_id,
            )
            tool_args = tool_input
            if block.name == "remember":
                if self._retrieval:
                    try:
                        _idb = (
                            getattr(self._handler, '_instance_db', None)
                            if hasattr(self, '_handler') and self._handler
                            else None
                        )
                        result = await self._retrieval.search(
                            request.instance_id,
                            tool_args.get("query", ""),
                            request.active_space_id,
                            requesting_member_id=getattr(request, "member_id", ""),
                            instance_db=_idb,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'remember' failed: %s", exc)
                        result = "Memory search failed — try asking in a different way."
                else:
                    result = "Memory search is not available right now."
            elif block.name == "remember_details":
                result = await self._handle_remember_details(
                    request.instance_id,
                    request.active_space_id,
                    tool_args,
                )
            elif block.name == "write_file":
                if self._files:
                    try:
                        result = await self._files.write_file(
                            request.instance_id,
                            request.active_space_id,
                            tool_args.get("name", ""),
                            tool_args.get("content", ""),
                            tool_args.get("description", ""),
                            target_space_id=tool_args.get("target_space_id"),
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
                            request.instance_id,
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
                            request.instance_id,
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
                            request.instance_id,
                            request.active_space_id,
                            tool_args.get("name", ""),
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'delete_file' failed: %s", exc)
                        result = "File deletion failed — try again."
                else:
                    result = "File system is not available right now."
            elif block.name == "execute_code":
                try:
                    import json as _json
                    from kernos.kernel.code_exec import execute_code as _exec_code
                    _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    _exec_result = await _exec_code(
                        instance_id=request.instance_id,
                        space_id=request.active_space_id,
                        code=tool_args.get("code", ""),
                        timeout_seconds=tool_args.get("timeout_seconds", 30),
                        write_file_name=tool_args.get("write_file"),
                        data_dir=_data_dir,
                    )
                    result = _json.dumps(_exec_result)
                except Exception as exc:
                    logger.warning("Kernel tool 'execute_code' failed: %s", exc)
                    result = f"Code execution failed: {exc}"
            elif block.name == "manage_workspace":
                if hasattr(self, '_workspace') and self._workspace:
                    try:
                        action = tool_args.get("action", "list")
                        if action == "list":
                            result = await self._workspace.list_artifacts(request.instance_id, request.active_space_id)
                        elif action == "add":
                            msg, _ = await self._workspace.add_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact", {}))
                            result = msg
                        elif action == "update":
                            result = await self._workspace.update_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact_id", ""), tool_args.get("artifact", {}))
                        elif action == "archive":
                            result = await self._workspace.archive_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact_id", ""))
                        else:
                            result = f"Unknown action: {action}"
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_workspace' failed: %s", exc)
                        result = f"Workspace operation failed: {exc}"
                else:
                    result = "Workspace manager is not available."
            elif block.name == "register_tool":
                if hasattr(self, '_workspace') and self._workspace:
                    try:
                        _desc_file = tool_args.get("descriptor_file", "") or tool_args
                        result = await self._workspace.register_tool(request.instance_id, request.active_space_id, _desc_file)
                    except Exception as exc:
                        logger.warning("Kernel tool 'register_tool' failed: %s", exc)
                        result = f"Registration failed: {exc}"
                else:
                    result = "Workspace manager is not available."
            elif block.name == "manage_plan":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_manage_plan(
                            request.instance_id, request.active_space_id, tool_args)
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_plan' failed: %s", exc)
                        result = f"Plan operation failed: {exc}"
                else:
                    result = "Self-directed execution is not available."
            elif block.name == "read_runtime_trace":
                if hasattr(self, '_handler') and self._handler:
                    _turns = tool_args.get("turns", 10)
                    _filter = tool_args.get("filter", None)
                    _turn_id = tool_args.get("turn_id", None)
                    events = await self._handler._runtime_trace.read(
                        request.instance_id, turns=_turns,
                        filter_level=_filter, turn_id=_turn_id)
                    if not events:
                        result = "No trace events found."
                    else:
                        lines = []
                        for e in events:
                            lines.append(
                                f"[{e.get('timestamp', '?')[:19]}] {e.get('level', '?').upper()} "
                                f"{e.get('source', '?')}:{e.get('event', '?')} — {e.get('detail', '')[:200]}"
                            )
                        result = f"Runtime trace ({len(events)} events):\n" + "\n".join(lines)
                else:
                    result = "Runtime trace is not available."
            elif block.name in ("diagnose_issue", "propose_fix", "submit_spec"):
                from kernos.kernel.diagnostics import handle_diagnose_issue, handle_propose_fix, handle_submit_spec
                _rt = getattr(self._handler, '_runtime_trace', None) if self._handler else None
                try:
                    if block.name == "diagnose_issue":
                        result = await handle_diagnose_issue(
                            request.instance_id, request.active_space_id, tool_args, _rt, self)
                    elif block.name == "propose_fix":
                        result = await handle_propose_fix(request.instance_id, tool_args, _rt)
                    else:
                        result = await handle_submit_spec(request.instance_id, tool_args, self._handler)
                except Exception as exc:
                    logger.warning("Kernel tool '%s' failed: %s", block.name, exc)
                    result = f"Diagnostic tool failed: {exc}"
            elif block.name == "manage_members":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_manage_members(
                            request.instance_id, tool_args, requesting_member_id=request.member_id)
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_members' failed: %s", exc)
                        result = f"Member management failed: {exc}"
                else:
                    result = "Member management is not available."
            elif block.name == "send_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_send_relational_message(
                            request.instance_id, tool_args,
                            origin_member_id=request.member_id,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'send_relational_message' failed: %s", exc)
                        result = f"Relational send failed: {exc}"
                else:
                    result = "Relational messaging is not available."
            elif block.name == "resolve_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_resolve_relational_message(
                            request.instance_id, tool_args,
                            requesting_member_id=request.member_id,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'resolve_relational_message' failed: %s", exc)
                        result = f"Relational resolve failed: {exc}"
                else:
                    result = "Relational messaging is not available."
            elif block.name == "dismiss_whisper":
                try:
                    result = await self._handle_dismiss_whisper(
                        request.instance_id,
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
                    soul = await self._state.get_soul(request.instance_id)
                    if soul:
                        from dataclasses import asdict
                        result = json.dumps(asdict(soul), indent=2)
                    else:
                        result = "No soul found for this instance."
                else:
                    result = "State store is not available."
            elif block.name == "update_soul":
                if self._state:
                    field_name = tool_args.get("field", "")
                    value = tool_args.get("value", "")
                    if field_name not in _SOUL_UPDATABLE_FIELDS:
                        result = (
                            f"Cannot update '{field_name}'. Only these fields can be updated: "
                            f"{', '.join(sorted(_SOUL_UPDATABLE_FIELDS))}."
                        )
                    else:
                        soul = await self._state.get_soul(request.instance_id)
                        if not soul:
                            result = "No soul found for this instance."
                        else:
                            setattr(soul, field_name, value)
                            await self._state.save_soul(soul, source="update_soul", trigger=f"{field_name}={value}")
                            result = f"Updated {field_name} to: {value}"
                else:
                    result = "State store is not available."
            elif block.name == "manage_covenants":
                try:
                    from kernos.kernel.covenant_manager import handle_manage_covenants
                    cov_action = tool_args.get("action", "list")
                    result = await handle_manage_covenants(
                        self._state,
                        request.instance_id,
                        action=cov_action,
                        rule_id=tool_args.get("rule_id", ""),
                        new_description=tool_args.get("new_description", ""),
                        show_all=tool_args.get("show_all", False),
                    )
                    if cov_action == "update" and "Updated" in result:
                        from kernos.kernel.covenant_manager import validate_covenant_set
                        id_match = re.search(r"new ID: (rule_\w+)", result)
                        new_id = id_match.group(1) if id_match else ""
                        if new_id:
                            asyncio.create_task(
                                validate_covenant_set(
                                    state=self._state,
                                    events=self._events,
                                    reasoning_service=self,
                                    instance_id=request.instance_id,
                                    new_rule_id=new_id,
                                )
                            )
                except Exception as exc:
                    logger.warning("Kernel tool 'manage_covenants' failed: %s", exc)
                    result = "Failed to manage covenants — try again."
            elif block.name == "manage_capabilities":
                try:
                    result = await self._handle_manage_capabilities(
                        request.instance_id,
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
            elif block.name == "send_to_channel":
                from kernos.kernel.channels import resolve_channel_alias
                _ch_input = tool_args.get("channel", "")
                _ch_msg = tool_args.get("message", "")
                if not _ch_input or not _ch_msg:
                    result = "Error: both 'channel' and 'message' are required."
                elif not self._channel_registry:
                    result = "Channel registry is not available."
                else:
                    _resolved = resolve_channel_alias(_ch_input)
                    _ch_info = self._channel_registry.get(_resolved)
                    if not _ch_info:
                        _avail = [c.name for c in self._channel_registry.get_connected()]
                        result = (
                            f"Channel '{_resolved}' (from '{_ch_input}') is not registered. "
                            f"Available channels: {', '.join(_avail) or 'none'}"
                        )
                    elif _ch_info.status != "connected":
                        result = f"Channel '{_resolved}' exists but is not connected (status: {_ch_info.status})."
                    elif not _ch_info.can_send_outbound:
                        result = f"Channel '{_resolved}' is connected but cannot send outbound messages."
                    elif not self._handler:
                        result = "Handler not available for outbound delivery."
                    else:
                        try:
                            from kernos.kernel.scheduler import resolve_owner_member_id as _resolve_mid
                            _member_id = _resolve_mid(request.instance_id)
                            await self._handler.send_outbound(
                                request.instance_id, _member_id, _resolved, _ch_msg,
                            )
                            logger.info(
                                "CROSS_CHANNEL_SEND: channel=%s resolved_from=%s len=%d",
                                _resolved, _ch_input, len(_ch_msg),
                            )
                            result = f"Message sent to {_ch_info.display_name}."
                        except Exception as exc:
                            result = f"Failed to send to {_resolved}: {exc}"
            elif block.name == "manage_schedule":
                from kernos.kernel.scheduler import handle_manage_schedule
                if self._trigger_store:
                    result = await handle_manage_schedule(
                        self._trigger_store,
                        request.instance_id,
                        member_id=request.active_space_id,
                        space_id=request.active_space_id,
                        action=tool_args.get("action", "list"),
                        trigger_id=tool_args.get("trigger_id", ""),
                        description=tool_args.get("description", ""),
                        reasoning_service=self,
                        conversation_id=request.conversation_id,
                        user_timezone=request.user_timezone,
                    )
                else:
                    result = "Scheduler is not available."
            elif block.name == "inspect_state":
                try:
                    from kernos.kernel.introspection import build_user_truth_view
                    result = await build_user_truth_view(
                        request.instance_id,
                        self._state,
                        self._trigger_store,
                        self._registry,
                    )
                except Exception as exc:
                    logger.warning("Kernel tool 'inspect_state' failed: %s", exc)
                    result = "State inspection failed — try again."
            elif block.name == "set_chain_model":
                # Admin-only: restrict to system space. The agent's Gate
                # already confines sensitive tools by space; here we apply a
                # lightweight space-check at dispatch time since Gate's admin
                # intent routing is a frozen primitive.
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "set_chain_model is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.setup.admin_tools import set_chain_model as _set_chain_model
                    try:
                        admin_res = _set_chain_model(
                            chain=tool_args.get("chain", ""),
                            provider_id=tool_args.get("provider_id", ""),
                            model_id=tool_args.get("model_id", ""),
                        )
                        result = admin_res.get("message") or admin_res.get("error") or "set_chain_model returned no result."
                    except Exception as exc:
                        logger.warning("Kernel tool 'set_chain_model' failed: %s", exc)
                        result = f"set_chain_model failed: {exc}"
            elif block.name == "diagnose_llm_chain":
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "diagnose_llm_chain is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.setup.admin_tools import diagnose_llm_chain as _diagnose_llm_chain
                    try:
                        import json as _json
                        admin_res = _diagnose_llm_chain(
                            include_fallback_events=bool(tool_args.get("include_fallback_events", False)),
                            instance_id=request.instance_id,
                        )
                        result = _json.dumps(admin_res, indent=2, default=str)
                    except Exception as exc:
                        logger.warning("Kernel tool 'diagnose_llm_chain' failed: %s", exc)
                        result = f"diagnose_llm_chain failed: {exc}"
            elif block.name == "diagnose_messenger":
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "diagnose_messenger is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.cohorts.admin import diagnose_messenger as _diagnose_messenger
                    try:
                        import json as _json
                        idb = getattr(self._handler, "_instance_db", None) if hasattr(self, "_handler") else None
                        admin_res = await _diagnose_messenger(
                            instance_id=request.instance_id,
                            member_a_id=tool_args.get("member_a_id", ""),
                            member_b_id=tool_args.get("member_b_id", ""),
                            state=self._state,
                            instance_db=idb,
                        )
                        result = _json.dumps(admin_res, indent=2, default=str)
                    except Exception as exc:
                        logger.warning("Kernel tool 'diagnose_messenger' failed: %s", exc)
                        result = f"diagnose_messenger failed: {exc}"
            elif block.name == "request_tool":
                result = await self._handle_request_tool(
                    request.instance_id,
                    request.active_space_id,
                    tool_args.get("capability_name", "unknown"),
                    tool_args.get("description", ""),
                )
            else:
                result = f"Kernel tool '{block.name}' not handled."
        else:
            # Lazy tool loading: check if this tool is a stub
            _tool_entry = None
            for _t in tools:
                if _t.get("name") == block.name:
                    _tool_entry = _t
                    break

            if _tool_entry and self._is_stub_schema(_tool_entry) and self._registry:
                full_schema = self._registry.get_tool_schema(block.name)
                if full_schema:
                    for _i, _t in enumerate(tools):
                        if _t.get("name") == block.name:
                            tools[_i] = full_schema
                            break
                    self.load_tool(request.active_space_id, block.name)
                    try:
                        if tool_effect in ("soft_write", "hard_write", "unknown"):
                            gate_cache[block.name] = gate_result  # noqa: F821
                    except NameError:
                        pass
                    logger.info(
                        "TOOL_LOAD: tool=%s space=%s (stub -> full schema, re-running)",
                        block.name, request.active_space_id,
                    )
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"[SYSTEM] The tool {block.name} is now fully loaded. "
                            "Please retry your call with the correct parameters."
                        ),
                    }

            if not _tool_entry and self._registry:
                schema = self._registry.get_tool_schema(block.name)
                if schema:
                    self.load_tool(request.active_space_id, block.name)
                    tools.append(schema)
                    logger.info(
                        "TOOL_LOAD: tool=%s space=%s (not in list, schema loaded)",
                        block.name, request.active_space_id,
                    )
                elif hasattr(self, '_workspace') and self._workspace and self._workspace._catalog and self._workspace._catalog.has_workspace_tool(block.name):
                    # Workspace tool — dispatch via workspace manager
                    _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    result = await self._workspace.execute_workspace_tool(
                        request.instance_id, block.name, tool_input, _data_dir)
                    logger.info("TOOL_DISPATCH: name=%s type=workspace", block.name)
                    tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
                    logger.info("AGENT_RESULT: tool=%s success=%s preview=%s",
                        block.name, "error" not in result.lower()[:50], result[:100])
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                else:
                    result = f"Tool '{block.name}' is not available."
                    tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
                    logger.info(
                        "AGENT_RESULT: tool=%s success=%s preview=%s",
                        block.name, False, result[:100],
                    )
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
            # Check workspace tools BEFORE MCP — workspace tools aren't in MCP
            if (hasattr(self, '_workspace') and self._workspace
                    and self._workspace._catalog
                    and self._workspace._catalog.has_workspace_tool(block.name)):
                _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                result = await self._workspace.execute_workspace_tool(
                    request.instance_id, block.name, tool_input, _data_dir)
                logger.info("TOOL_DISPATCH: name=%s type=workspace", block.name)
            else:
                result = await self._mcp.call_tool(block.name, tool_input)
                _is_mcp_tool = True

        tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
        is_error = result.startswith("Tool error:") or result.startswith(
            "Calendar tool error:")

        logger.info(
            "AGENT_RESULT: tool=%s success=%s preview=%s",
            block.name, not is_error, result[:100],
        )
        if is_error:
            self._trace(request, "warning", "reasoning", "TOOL_FAILED",
                f"tool={block.name} result={result[:200]}", phase="reason")

        # Emit tool.result
        try:
            await emit_event(
                self._events,
                EventType.TOOL_RESULT,
                request.instance_id,
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
            request.instance_id,
            {
                "type": "tool_result",
                "timestamp": utc_now(),
                "instance_id": request.instance_id,
                "conversation_id": request.conversation_id,
                "tool_name": block.name,
                "tool_output": str(result)[:2000],
            },
        )

        # Tool result budgeting: persist oversized MCP results
        injected = result
        if (
            _is_mcp_tool
            and not is_error
            and len(result) > TOOL_RESULT_CHAR_BUDGET
            and self._files
            and request.active_space_id
        ):
            ts = re.sub(r"[^0-9T-]", "", utc_now()[:19])
            slug = uuid.uuid4().hex[:6]
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", block.name)
            filename = f"tr_{safe_name}_{ts}_{slug}.txt"
            try:
                write_msg = await self._files.write_file(
                    request.instance_id,
                    request.active_space_id,
                    filename,
                    result,
                    f"Persisted tool result from {block.name} ({len(result)} chars)",
                )
                if write_msg.startswith("Error:"):
                    raise RuntimeError(write_msg)
                preview_chars = TOOL_RESULT_CHAR_BUDGET - 200
                raw_preview = result[:preview_chars]
                cleaned = re.sub(r"\n{3,}", "\n\n", raw_preview).rstrip()
                injected = (
                    f"[Tool result from {block.name} — {len(result)} chars, persisted]\n"
                    f"{cleaned}\n"
                    f"...\n"
                    f"[Full result saved as {filename}. "
                    f"Use read_file to access the full content.]"
                )
                logger.info(
                    "RESULT_BUDGETED: tool=%s original=%d preview=%d path=%s",
                    block.name, len(result), len(injected), filename,
                )
            except Exception as exc:
                logger.warning(
                    "Result budgeting failed, injecting raw: tool=%s err=%s",
                    block.name, exc,
                )

        # Accumulate trace for friction observer + tool receipts
        self._turn_tool_trace.append({
            "name": block.name,
            "input": tool_input,
            "success": not is_error,
            "result_preview": result[:200] if isinstance(result, str) else str(result)[:200],
        })

        # EVENT-STREAM-TO-SQLITE: one emission per tool invocation
        # (tool.returned on success, tool.failed on error). The inputs'
        # shape — not the inputs themselves — is captured so the payload
        # stays small and doesn't leak sensitive arguments.
        try:
            from kernos.kernel import event_stream
            _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
            _event_type = "tool.failed" if is_error else "tool.returned"
            await event_stream.emit(
                request.instance_id, _event_type,
                {
                    "name": block.name,
                    "args_keys": sorted((tool_input or {}).keys()),
                    "result_preview_len": len(str(result)[:200]) if result else 0,
                },
                member_id=request.member_id or None,
                space_id=request.active_space_id or None,
                correlation_id=_turn_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit tool event: %s", exc)

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": injected,
        }

    async def _handle_request_tool(
        self,
        instance_id: str,
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
                await self._activate_tool_for_space(instance_id, space_id, capability_name)
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
            await self._activate_tool_for_space(instance_id, space_id, best_match.name)
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
        self, instance_id: str, space_id: str, capability_name: str
    ) -> None:
        """Add a capability to a space's active_tools list and persist."""
        if not self._state:
            return
        space = await self._state.get_context_space(instance_id, space_id)
        if space and capability_name not in space.active_tools:
            space.active_tools.append(capability_name)
            await self._state.update_context_space(
                instance_id, space_id, {"active_tools": space.active_tools}
            )

    async def _handle_manage_capabilities(
        self, instance_id: str, action: str, capability: str
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
                instance_id, "", capability, f"Install {capability}"
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

    async def _handle_canvas_tool(
        self, tool_name: str, tool_input: dict, request: "ReasoningRequest",
    ) -> str:
        """Dispatch canvas_*/page_* tool calls to CanvasService.

        Pillar 3 of CANVAS-V1. Consent-on-cross-member-writes lives here
        (at the tool layer, above the dispatch gate): page_write to a
        cross-member non-log page without ``confirmed=true`` short-circuits
        and tells the agent to re-ask the user.
        """
        # Lazy-resolve the canvas service via the handler. Keeps the
        # wire-up simple: server.py/bootstrap attach _instance_db to the
        # handler post-init, and the first canvas tool call constructs
        # the service on demand.
        canvas = self._canvas
        if canvas is None and self._handler and hasattr(self._handler, "_get_canvas_service"):
            canvas = self._handler._get_canvas_service()
            if canvas is not None:
                self._canvas = canvas
        if not canvas:
            return "Canvas service is not available."

        import json as _json

        instance_id = request.instance_id
        member_id = getattr(request, "member_id", "") or ""

        async def _assert_access(canvas_id: str) -> str | None:
            idb = getattr(self._handler, "_instance_db", None) if self._handler else None
            if not idb:
                return "Instance database is not available."
            ok = await idb.member_has_canvas_access(
                canvas_id=canvas_id, member_id=member_id,
            )
            if not ok:
                return _json.dumps({
                    "ok": False,
                    "error": "canvas_not_accessible",
                    "detail": f"Canvas {canvas_id!r} does not exist or is not accessible.",
                })
            return None

        if tool_name == "canvas_list":
            include_archived = bool(tool_input.get("include_archived", False))
            canvases = await self._canvas.list_for_member(
                member_id=member_id, include_archived=include_archived,
            )
            return _json.dumps({"ok": True, "canvases": canvases}, default=str)

        if tool_name == "canvas_create":
            result = await self._canvas.create(
                instance_id=instance_id,
                creator_member_id=member_id,
                name=tool_input.get("name", ""),
                scope=tool_input.get("scope", ""),
                members=tool_input.get("members") or [],
                description=tool_input.get("description", ""),
                default_page_type=tool_input.get("default_page_type", "note"),
                pinned_to_spaces=tool_input.get("pinned_to_spaces") or [],
            )
            return _json.dumps(result.to_dict(), default=str)

        if tool_name == "page_read":
            canvas_id = tool_input.get("canvas_id", "")
            err = await _assert_access(canvas_id)
            if err:
                return err
            result = await self._canvas.page_read(
                instance_id=instance_id,
                canvas_id=canvas_id,
                page_slug=tool_input.get("page_path", ""),
            )
            return _json.dumps(result.to_dict(), default=str)

        if tool_name == "page_list":
            canvas_id = tool_input.get("canvas_id", "")
            err = await _assert_access(canvas_id)
            if err:
                return err
            pages = await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            return _json.dumps({"ok": True, "canvas_id": canvas_id, "pages": pages}, default=str)

        if tool_name == "page_search":
            query = tool_input.get("query", "")
            canvas_id = tool_input.get("canvas_id") or ""
            limit = int(tool_input.get("limit", 20) or 20)
            if canvas_id:
                err = await _assert_access(canvas_id)
                if err:
                    return err
                canvas_ids = [canvas_id]
            else:
                canvases = await self._canvas.list_for_member(member_id=member_id)
                canvas_ids = [c["canvas_id"] for c in canvases if c.get("canvas_id")]
            hits = await self._canvas.page_search(
                instance_id=instance_id,
                canvas_ids=canvas_ids,
                query=query,
                limit=limit,
            )
            return _json.dumps({"ok": True, "hits": hits}, default=str)

        if tool_name == "page_write":
            canvas_id = tool_input.get("canvas_id", "")
            page_slug = tool_input.get("page_path", "")
            err = await _assert_access(canvas_id)
            if err:
                return err

            # Consent gate for cross-member shared writes.
            # Scope: team or specific canvases with >1 member AND non-log page
            # writes require explicit confirmed=true. Personal canvases and
            # log pages skip this — personal is solo, logs are append-only.
            idb = getattr(self._handler, "_instance_db", None) if self._handler else None
            canvas_row = await idb.get_canvas(canvas_id) if idb else None
            canvas_scope = (canvas_row or {}).get("scope", "") if canvas_row else ""
            page_type = (tool_input.get("page_type") or "note").lower()
            confirmed = bool(tool_input.get("confirmed", False))

            is_cross_member = canvas_scope in ("team", "specific")
            requires_consent = (
                is_cross_member
                and page_type != "log"
                and not confirmed
            )
            if requires_consent:
                members = await idb.list_canvas_members(canvas_id) if idb else []
                other_members = [m for m in members if m and m != member_id]
                return _json.dumps({
                    "ok": False,
                    "requires_confirmation": True,
                    "canvas_id": canvas_id,
                    "page_path": page_slug,
                    "scope": canvas_scope,
                    "other_members": other_members,
                    "proposed_summary": (tool_input.get("body") or "")[:200],
                    "detail": (
                        "This is a shared canvas with other members. Surface the "
                        "proposed write to the user and re-call page_write with "
                        "confirmed=true after they approve."
                    ),
                })

            result = await self._canvas.page_write(
                instance_id=instance_id,
                canvas_id=canvas_id,
                page_slug=page_slug,
                body=tool_input.get("body", ""),
                writer_member_id=member_id,
                title=tool_input.get("title"),
                page_type=tool_input.get("page_type"),
                state=tool_input.get("state"),
            )
            return _json.dumps(result.to_dict(), default=str)

        return f"Canvas tool {tool_name!r} not dispatched."

    async def _handle_dismiss_whisper(
        self, instance_id: str, whisper_id: str, reason: str = "user_dismissed"
    ) -> str:
        """Dismiss a whisper — update suppression to prevent re-surfacing."""
        if not self._state:
            return "State store is not available."
        suppressions = await self._state.get_suppressions(
            instance_id, whisper_id=whisper_id
        )
        if suppressions:
            s = suppressions[0]
            s.resolution_state = "dismissed"
            s.resolved_by = reason
            s.resolved_at = datetime.now(timezone.utc).isoformat()
            await self._state.save_suppression(instance_id, s)

            # If this was a behavioral pattern whisper, mark pattern as declined
            if s.foresight_signal.startswith("behavioral_pattern:"):
                try:
                    _bp_id = s.foresight_signal.split(":", 1)[1]
                    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    from kernos.kernel.behavioral_patterns import load_patterns, save_patterns
                    patterns = load_patterns(data_dir, instance_id)
                    for p in patterns:
                        if p.pattern_id == _bp_id:
                            p.proposal_declined = True
                            p.proposal_surfaced = False  # Allow re-proposal after reset
                            p.threshold_met = False
                            save_patterns(data_dir, instance_id, patterns)
                            logger.info("BEHAVIORAL_RESOLVED: fingerprint=%s action=declined", p.fingerprint[:40])
                            break
                except Exception as exc:
                    logger.debug("BEHAVIORAL_PATTERN: decline handling failed: %s", exc)

            return f"Dismissed whisper {whisper_id}. Won't bring this up again."
        return f"Whisper {whisper_id} not found in suppression registry."

    async def _handle_remember_details(
        self, instance_id: str, space_id: str, input_data: dict,
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

        # Read via HandlerProtocol.read_log_text
        if not self._handler or not hasattr(self._handler, "read_log_text"):
            return "Conversation logger is not available."

        log_text = await self._handler.read_log_text(
            instance_id, space_id, log_number,
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
                request.instance_id,
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

        # Token estimation: hybrid (real baseline + delta) when available, char-based fallback.
        _tool_chars = sum(len(json.dumps(t)) for t in tools)
        _ctx_chars = len(request.system_prompt) + sum(
            len(m.get("content", "") if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", "")))
            for m in messages
        )
        _char_est = (_ctx_chars + _tool_chars) // 4
        _last_real = self._last_real_input_tokens.get(request.instance_id, 0)
        if _last_real > 0:
            # Hybrid: real baseline + estimated delta from new content
            # The last real count covers the full context window at that point.
            # Delta is new user message + any changed context (estimated).
            _new_content_chars = len(request.input_text or "")
            _delta_est = _new_content_chars // 4
            _ctx_tokens_est = _last_real + _delta_est
        else:
            _ctx_tokens_est = _char_est

        _tool_sizes = [(t.get("name", "?"), len(json.dumps(t))) for t in tools]
        _tool_sizes.sort(key=lambda x: x[1], reverse=True)
        _tool_tokens = sum(chars // 4 for _, chars in _tool_sizes)
        _top3 = ", ".join(f"{name}={chars//4}tok" for name, chars in _tool_sizes[:3])
        logger.info(
            "REASON_START: tool_count=%d tool_tokens=%d max_tokens=%d msg_count=%d "
            "ctx_tokens_est=%d (hybrid=%d char=%d real_baseline=%d) top_tools=[%s]",
            len(tools), _tool_tokens, request.max_tokens, len(messages), _ctx_tokens_est,
            _ctx_tokens_est, _char_est, _last_real, _top3,
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
        # Build cache-boundary system prompt if static/dynamic split is available
        if request.system_prompt_static:
            _system: str | list[dict] = [
                {"type": "text", "text": request.system_prompt_static, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": request.system_prompt_dynamic},
            ]
        else:
            _system = request.system_prompt
        response = await self._call_chain(
            "primary", _system, messages, tools, request.max_tokens,
            request_model=request.model, request=request,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        # Store real input_tokens for hybrid estimation on next turn
        # This is the initial API call (full context window), not tool-loop iterations
        if response.input_tokens > 0:
            self._last_real_input_tokens[request.instance_id] = response.input_tokens

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
                request.instance_id,
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
        _is_plan_step = (request.input_text or "").startswith("[PLAN STEP")
        _max_iters = self.MAX_TOOL_ITERATIONS_PLAN if _is_plan_step else self.MAX_TOOL_ITERATIONS
        _gate_cache: dict[str, Any] = {}  # tool_name → GateResult (for lazy-load re-runs)
        while (
            response.stop_reason == "tool_use"
            and iterations < _max_iters
        ):
            iterations += 1
            tool_results: list[dict] = []

            # Build a per-tool-call index of agent reasoning.
            _last_text = "No explicit reasoning provided."
            _tool_reasoning: dict[str, str] = {}
            for _b in response.content:
                if _b.type == "text" and _b.text:
                    _last_text = _b.text.strip() or "No explicit reasoning provided."
                elif _b.type == "tool_use" and _b.id:
                    _tool_reasoning[_b.id] = _last_text
                    _last_text = "No explicit reasoning provided."

            # Collect all tool_use blocks with their original index
            indexed_blocks: list[tuple[int, ContentBlock]] = []
            for i, block in enumerate(response.content):
                if block.type == "tool_use":
                    indexed_blocks.append((i, block))

            # Pre-pass: handle stubs (modifies shared tools list — must be sequential)
            stub_results: dict[int, dict] = {}
            non_stub_blocks: list[tuple[int, ContentBlock]] = []
            for idx, block in indexed_blocks:
                if block.name not in self._KERNEL_TOOLS:
                    _stub_entry = None
                    for _t in tools:
                        if _t.get("name") == block.name:
                            _stub_entry = _t
                            break
                    if _stub_entry and self._is_stub_schema(_stub_entry) and self._registry:
                        full_schema = self._registry.get_tool_schema(block.name)
                        if full_schema:
                            for _i, _t in enumerate(tools):
                                if _t.get("name") == block.name:
                                    tools[_i] = full_schema
                                    break
                            self.load_tool(request.active_space_id, block.name)
                            logger.info(
                                "TOOL_LOAD: tool=%s space=%s (stub -> full schema, re-running)",
                                block.name, request.active_space_id,
                            )
                            # Build a hint about required parameters from the full schema
                            _required = full_schema.get("input_schema", {}).get("required", [])
                            _props = full_schema.get("input_schema", {}).get("properties", {})
                            _param_hint = ""
                            if _required:
                                _param_hint = f" Required parameters: {', '.join(_required)}."
                            elif _props:
                                _param_hint = f" Available parameters: {', '.join(list(_props.keys())[:8])}."
                            stub_results[idx] = {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": (
                                    f"[SYSTEM] The tool {block.name} is now fully loaded with its complete schema. "
                                    f"Your previous call had empty parameters because the schema wasn't loaded yet."
                                    f"{_param_hint} "
                                    f"Please call {block.name} again with the correct parameters for the user's request."
                                ),
                            }
                            continue
                non_stub_blocks.append((idx, block))

            # Classify non-stub blocks into concurrent-safe vs sequential
            concurrent: list[tuple[int, ContentBlock]] = []
            sequential: list[tuple[int, ContentBlock]] = []
            for idx, block in non_stub_blocks:
                if self._is_concurrent_safe(block.name):
                    concurrent.append((idx, block))
                else:
                    sequential.append((idx, block))

            results_by_index: dict[int, dict] = dict(stub_results)

            # Log concurrency decision when multiple tools present
            total_tools = len(indexed_blocks)
            if total_tools > 1:
                logger.info(
                    "TOOL_CONCURRENT: parallel=%d sequential=%d stubs=%d total=%d",
                    len(concurrent), len(sequential), len(stub_results), total_tools,
                )

            # Execute concurrent-safe (read) tools in parallel
            if concurrent:
                async def _run_concurrent(
                    idx: int, block: ContentBlock,
                ) -> tuple[int, dict]:
                    tool_input = dict(block.input or {})
                    agent_reasoning = _tool_reasoning.get(
                        block.id or "", "No explicit reasoning provided.")
                    logger.info(
                        "TOOL_LOOP iter=%d tool=%s kernel=%s",
                        iterations, block.name, block.name in self._KERNEL_TOOLS,
                    )
                    try:
                        tr = await self._execute_single_tool(
                            block, tool_input, request, tools,
                            _gate_cache, rr_event, iterations, agent_reasoning,
                        )
                    except Exception as exc:
                        logger.warning("Concurrent tool error: tool=%s err=%s", block.name, exc)
                        tr = {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Tool error: {exc}",
                        }
                    return idx, tr

                gather_results = await asyncio.gather(
                    *[_run_concurrent(idx, block) for idx, block in concurrent],
                    return_exceptions=True,
                )
                for item in gather_results:
                    if isinstance(item, Exception):
                        logger.warning("Concurrent gather exception: %s", item)
                        continue
                    c_idx, c_result = item
                    results_by_index[c_idx] = c_result

            # Execute sequential (write/unknown) tools one at a time
            for idx, block in sequential:
                tool_input = dict(block.input or {})
                agent_reasoning = _tool_reasoning.get(
                    block.id or "", "No explicit reasoning provided.")
                logger.info(
                    "TOOL_LOOP iter=%d tool=%s kernel=%s",
                    iterations, block.name, block.name in self._KERNEL_TOOLS,
                )
                tr = await self._execute_single_tool(
                    block, tool_input, request, tools,
                    _gate_cache, rr_event, iterations, agent_reasoning,
                )
                results_by_index[idx] = tr

            # Emit tool_results in original block order
            tool_results = [results_by_index[idx] for idx, _ in indexed_blocks]

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
                    request.instance_id,
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
            response = await self._call_chain(
                "primary", _system, messages, tools, request.max_tokens,
                request_model=request.model, request=request,
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
                    request.instance_id,
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

        if iterations >= _max_iters:
            logger.warning("TOOL_LOOP EXHAUSTED after %d iterations (limit=%d plan=%s)",
                iterations, _max_iters, _is_plan_step)
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
                # Agent claims it PERFORMED an action (first person + past tense)
                # These indicate the agent believes it did something, not that it's
                # discussing a tool conceptually. Mentioning a tool by name without
                # claiming action is NOT a hallucination signal.
                "i created", "i deleted", "i wrote", "i removed",
                "i've created", "i've deleted", "i've written", "i've removed",
                "i scheduled", "i've scheduled", "i set a reminder",
                "i've set a reminder",
                "i sent", "i've sent",
                # Completion claims at start of response
                "done —", "done.", "✅",
                # Schedule-specific: agent describes a COMPLETED future action
                "reminder set", "event created",
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
