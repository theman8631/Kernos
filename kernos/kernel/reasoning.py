"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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

        if iterations >= self.MAX_TOOL_ITERATIONS:
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
