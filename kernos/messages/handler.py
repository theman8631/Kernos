import logging
import os
import time
from datetime import datetime, timezone

import anthropic

from kernos.capability.client import MCPClientManager
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event, estimate_cost
from kernos.kernel.state import (
    ConversationSummary,
    StateStore,
    TenantProfile,
    default_contract_rules,
)
from kernos.messages.models import NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, TenantStore, derive_tenant_id

# Handler knows about NormalizedMessage, MCPClientManager, persistence stores,
# EventStream, and StateStore. It knows nothing about platform adapters.

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_PROVIDER = "anthropic"

_PLATFORM_CONTEXT: dict[str, str] = {
    "sms": (
        "You are communicating via SMS. Keep responses very short — "
        "a few sentences max unless the user asks for detail."
    ),
    "discord": (
        "You are communicating via Discord. Keep responses concise and clear; "
        "you can use a paragraph or two when the topic warrants it."
    ),
}

_AUTH_CONTEXT: dict[str, str] = {
    "owner_verified": (
        "The person you're talking to is the verified owner of this Kernos instance."
    ),
    "owner_unverified": (
        "The sender's phone number matches the owner but is not fully verified "
        "(phone numbers can be spoofed)."
    ),
    "unknown": (
        "This is an unrecognized sender. Be helpful but do not share any private information."
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_system_prompt(
    message: NormalizedMessage, tools: list[dict] | None = None
) -> str:
    """Build a platform-aware, capability-honest system prompt.

    The CURRENT CAPABILITIES section is built dynamically from the tool list
    so the agent never claims a capability that isn't backed by a real tool.
    """
    platform_line = _PLATFORM_CONTEXT.get(
        message.platform,
        f"You are communicating via {message.platform}. Keep responses concise.",
    )
    auth_line = _AUTH_CONTEXT.get(
        message.sender_auth_level.value,
        f"Sender auth level: {message.sender_auth_level.value}.",
    )

    tools = tools or []
    if tools:
        tool_names = [t["name"] for t in tools]
        has_calendar = any(
            "calendar" in n.lower() or "event" in n.lower() for n in tool_names
        )
        if has_calendar:
            capabilities = (
                "CURRENT CAPABILITIES — only claim these:\n"
                "- Conversation: answer questions, discuss topics, help think through problems.\n"
                "- Google Calendar: check your schedule, list events, find availability. "
                "Always use calendar tools when asked about schedule, events, or appointments — "
                "never guess from memory.\n"
                "You cannot set reminders, send emails, do web research, manage files, "
                "or take other actions. "
                "Be honest about limits — more capabilities are coming."
            )
        else:
            tool_list = ", ".join(tool_names)
            capabilities = (
                "CURRENT CAPABILITIES — only claim these:\n"
                "- Conversation: answer questions, discuss topics, help think through problems.\n"
                f"- Tools available: {tool_list}.\n"
                "You cannot do anything beyond what the available tools provide. "
                "Be honest about limits."
            )
    else:
        capabilities = (
            "CURRENT CAPABILITIES — only claim these:\n"
            "- Conversation: answer questions, discuss topics, help think through problems, brainstorm ideas.\n"
            "That is ALL you can do right now. You cannot check calendars, set reminders, send emails, "
            "do web research, manage files, or take any actions. "
            "If asked about these, be honest that you don't have those capabilities yet — "
            "don't pretend or make things up. It's fine to mention that more capabilities are coming."
        )

    return (
        "You are Kernos, a personal intelligence assistant. "
        "You are in early development. Be honest about what you can and cannot do.\n\n"
        "You have conversation memory — you can see recent messages from this conversation, "
        "even across restarts. You don't have memory across different conversations or channels yet.\n\n"
        f"{platform_line}\n\n"
        f"{auth_line}\n\n"
        f"{capabilities}"
    )


class MessageHandler:
    """Receives NormalizedMessages, brokers tool calls via MCP, returns response strings.

    The handler reasons about the current moment. The kernel (stores + event stream)
    captures everything that happens.
    """

    MAX_TOOL_ITERATIONS = 10

    def __init__(
        self,
        mcp: MCPClientManager,
        conversations: ConversationStore,
        tenants: TenantStore,
        audit: AuditStore,
        events: EventStream,
        state: StateStore,
    ) -> None:
        self.mcp = mcp
        self.conversations = conversations
        self.tenants = tenants
        self.audit = audit
        self.events = events
        self.state = state
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    async def _ensure_tenant_state(
        self, tenant_id: str, message: NormalizedMessage
    ) -> None:
        """Create StateStore profile and seed default contracts for new tenants."""
        profile = await self.state.get_tenant_profile(tenant_id)
        if profile is not None:
            return

        now = _now_iso()
        new_profile = TenantProfile(
            tenant_id=tenant_id,
            status="active",
            created_at=now,
            platforms={
                message.platform: {"connected_at": now, "sender": message.sender}
            },
            preferences={},
            capabilities={},
            model_config={"default_provider": _PROVIDER, "quality_tier": 3},
        )
        await self.state.save_tenant_profile(tenant_id, new_profile)

        for rule in default_contract_rules(tenant_id, now):
            await self.state.add_contract_rule(rule)

        try:
            await emit_event(
                self.events,
                EventType.TENANT_PROVISIONED,
                tenant_id,
                "handler",
                payload={"platform": message.platform, "sender": message.sender},
            )
        except Exception as exc:
            logger.warning("Failed to emit tenant.provisioned: %s", exc)

        logger.info("Provisioned state for new tenant: %s", tenant_id)

    async def _update_conversation_summary(
        self, tenant_id: str, conversation_id: str, platform: str
    ) -> None:
        now = _now_iso()
        try:
            summary = await self.state.get_conversation_summary(
                tenant_id, conversation_id
            )
            if summary is None:
                summary = ConversationSummary(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    platform=platform,
                    message_count=1,
                    first_message_at=now,
                    last_message_at=now,
                )
            else:
                summary.message_count += 1
                summary.last_message_at = now
            await self.state.save_conversation_summary(summary)
        except Exception as exc:
            logger.warning("Failed to update conversation summary: %s", exc)

    async def process(self, message: NormalizedMessage) -> str:
        """Process a NormalizedMessage and return a response string.

        Flow:
        1. Derive tenant_id and provision if new (TenantStore + StateStore)
        2. Load recent conversation history
        3. Emit message.received
        4. Store user message (before API call)
        5. Emit reasoning.request; call Claude; emit reasoning.response
        6. Tool-use loop: emit tool.called/tool.result alongside existing audit.log
        7. Store assistant response; emit message.sent; update conversation summary
        """
        tenant_id = derive_tenant_id(message)
        conversation_id = message.conversation_id

        # Steps 1–2: provision and load history
        await self.tenants.get_or_create(tenant_id)
        await self._ensure_tenant_state(tenant_id, message)
        history = await self.conversations.get_recent(
            tenant_id, conversation_id, limit=20
        )

        # Emit message.received
        try:
            await emit_event(
                self.events,
                EventType.MESSAGE_RECEIVED,
                tenant_id,
                "handler",
                payload={
                    "content": message.content,
                    "sender": message.sender,
                    "sender_auth_level": message.sender_auth_level.value,
                    "platform": message.platform,
                    "conversation_id": conversation_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit message.received: %s", exc)

        # Store user message before the API call (the event happened even if Claude fails)
        user_entry = {
            "role": "user",
            "content": message.content,
            "timestamp": message.timestamp.isoformat(),
            "platform": message.platform,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        }
        await self.conversations.append(tenant_id, conversation_id, user_entry)

        # Build messages array: history + current user message
        tools = self.mcp.get_tools()
        system_prompt = _build_system_prompt(message, tools)
        messages: list[dict] = history + [{"role": "user", "content": message.content}]

        try:
            # Emit reasoning.request
            await emit_event(
                self.events,
                EventType.REASONING_REQUEST,
                tenant_id,
                "handler",
                payload={
                    "model": _MODEL,
                    "provider": _PROVIDER,
                    "conversation_id": conversation_id,
                    "message_count": len(messages),
                    "tool_count": len(tools),
                    "system_prompt_length": len(system_prompt),
                    "trigger": "user_message",
                },
            )

            t0 = time.monotonic()
            response = self.client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
                tools=tools if tools else anthropic.NOT_GIVEN,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)

            # Emit reasoning.response with token counts and cost
            rr_event = await emit_event(
                self.events,
                EventType.REASONING_RESPONSE,
                tenant_id,
                "handler",
                payload={
                    "model": _MODEL,
                    "provider": _PROVIDER,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "estimated_cost_usd": estimate_cost(
                        _MODEL, response.usage.input_tokens, response.usage.output_tokens
                    ),
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    "conversation_id": conversation_id,
                },
            )

            # Tool-use loop
            iterations = 0
            while (
                response.stop_reason == "tool_use"
                and iterations < self.MAX_TOOL_ITERATIONS
            ):
                iterations += 1
                assistant_content = response.content
                tool_results = []

                for block in assistant_content:
                    if block.type == "tool_use":
                        # Emit tool.called
                        await emit_event(
                            self.events,
                            EventType.TOOL_CALLED,
                            tenant_id,
                            "handler",
                            payload={
                                "tool_name": block.name,
                                "tool_input": block.input,
                                "conversation_id": conversation_id,
                                "reasoning_event_id": rr_event.id,
                            },
                        )
                        # Existing audit log
                        await self.audit.log(
                            tenant_id,
                            {
                                "type": "tool_call",
                                "timestamp": _now_iso(),
                                "tenant_id": tenant_id,
                                "conversation_id": conversation_id,
                                "tool_name": block.name,
                                "tool_input": block.input,
                            },
                        )

                        t_tool = time.monotonic()
                        result = await self.mcp.call_tool(block.name, block.input)
                        tool_duration_ms = int((time.monotonic() - t_tool) * 1000)

                        is_error = result.startswith("Tool error:") or result.startswith(
                            "Calendar tool error:"
                        )
                        # Emit tool.result
                        await emit_event(
                            self.events,
                            EventType.TOOL_RESULT,
                            tenant_id,
                            "handler",
                            payload={
                                "tool_name": block.name,
                                "success": not is_error,
                                "result_length": len(result),
                                "duration_ms": tool_duration_ms,
                                "conversation_id": conversation_id,
                                "error": result if is_error else None,
                            },
                        )
                        # Existing audit log
                        await self.audit.log(
                            tenant_id,
                            {
                                "type": "tool_result",
                                "timestamp": _now_iso(),
                                "tenant_id": tenant_id,
                                "conversation_id": conversation_id,
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

                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})

                # Emit reasoning.request for tool continuation
                await emit_event(
                    self.events,
                    EventType.REASONING_REQUEST,
                    tenant_id,
                    "handler",
                    payload={
                        "model": _MODEL,
                        "provider": _PROVIDER,
                        "conversation_id": conversation_id,
                        "message_count": len(messages),
                        "tool_count": len(tools),
                        "system_prompt_length": len(system_prompt),
                        "trigger": "tool_continuation",
                    },
                )

                t0 = time.monotonic()
                response = self.client.messages.create(
                    model=_MODEL,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                rr_event = await emit_event(
                    self.events,
                    EventType.REASONING_RESPONSE,
                    tenant_id,
                    "handler",
                    payload={
                        "model": _MODEL,
                        "provider": _PROVIDER,
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "estimated_cost_usd": estimate_cost(
                            _MODEL,
                            response.usage.input_tokens,
                            response.usage.output_tokens,
                        ),
                        "stop_reason": response.stop_reason,
                        "duration_ms": duration_ms,
                        "conversation_id": conversation_id,
                    },
                )

            if iterations >= self.MAX_TOOL_ITERATIONS:
                return "I'm having trouble completing that request. Try asking in a simpler way."

            text_parts = [
                block.text for block in response.content if block.type == "text"
            ]
            response_text = (
                "".join(text_parts)
                if text_parts
                else "I processed your request but don't have a text response. Try rephrasing?"
            )

            # Store assistant response
            assistant_entry = {
                "role": "assistant",
                "content": response_text,
                "timestamp": _now_iso(),
                "platform": message.platform,
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
            }
            await self.conversations.append(tenant_id, conversation_id, assistant_entry)

            # Emit message.sent
            await emit_event(
                self.events,
                EventType.MESSAGE_SENT,
                tenant_id,
                "handler",
                payload={
                    "content": response_text,
                    "conversation_id": conversation_id,
                    "platform": message.platform,
                    "reasoning_event_id": rr_event.id,
                },
            )

            await self._update_conversation_summary(tenant_id, conversation_id, message.platform)

            return response_text

        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            logger.error(
                "Claude API connection/timeout error for sender=%s: %s",
                message.sender,
                exc,
                exc_info=True,
            )
            try:
                await emit_event(
                    self.events,
                    EventType.HANDLER_ERROR,
                    tenant_id,
                    "handler",
                    payload={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": conversation_id,
                        "stage": "api_call",
                    },
                )
            except Exception:
                pass
            return "Something went wrong on my end — try again in a moment."

        except anthropic.RateLimitError as exc:
            logger.error(
                "Claude API rate limit hit for sender=%s: %s",
                message.sender,
                exc,
                exc_info=True,
            )
            try:
                await emit_event(
                    self.events,
                    EventType.HANDLER_ERROR,
                    tenant_id,
                    "handler",
                    payload={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": conversation_id,
                        "stage": "api_call",
                    },
                )
            except Exception:
                pass
            return "I'm a bit overloaded right now. Try again in a minute."

        except anthropic.APIStatusError as exc:
            logger.error(
                "Claude API status error %s for sender=%s: %s",
                exc.status_code,
                message.sender,
                exc,
                exc_info=True,
            )
            try:
                await emit_event(
                    self.events,
                    EventType.HANDLER_ERROR,
                    tenant_id,
                    "handler",
                    payload={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": conversation_id,
                        "stage": "api_call",
                    },
                )
            except Exception:
                pass
            return "Something went wrong on my end — try again in a moment."

        except Exception as exc:
            logger.error(
                "Unexpected error in handler for sender=%s: %s",
                message.sender,
                exc,
                exc_info=True,
            )
            try:
                await emit_event(
                    self.events,
                    EventType.HANDLER_ERROR,
                    tenant_id,
                    "handler",
                    payload={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": conversation_id,
                        "stage": "general",
                    },
                )
            except Exception:
                pass
            return "Something unexpected happened. Try again, and if it keeps happening, let me know."
