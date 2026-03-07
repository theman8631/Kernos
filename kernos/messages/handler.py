import logging
from datetime import datetime, timezone

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService
from kernos.kernel.projectors.coordinator import run_projectors
from kernos.kernel.soul import Soul
from kernos.kernel.task import Task, TaskType, generate_task_id
from kernos.kernel.template import AgentTemplate, PRIMARY_TEMPLATE
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    CovenantRule,
    ConversationSummary,
    StateStore,
    TenantProfile,
    default_covenant_rules,
)
# Backwards-compat aliases used elsewhere in this module
ContractRule = CovenantRule
default_contract_rules = default_covenant_rules
from kernos.messages.models import NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, TenantStore, derive_tenant_id

# Handler knows about NormalizedMessage, MCPClientManager, persistence stores,
# EventStream, StateStore, ReasoningService, and CapabilityRegistry.
# It knows nothing about platform adapters.

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_PROVIDER = "anthropic"

# Minimum interaction count before bootstrap graduation is even evaluated.
_BOOTSTRAP_MIN_INTERACTIONS = 10

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


def _format_contracts(rules: list[CovenantRule]) -> str:
    """Format behavioral contract rules into natural language for the system prompt."""
    if not rules:
        return ""
    lines = ["BEHAVIORAL CONTRACTS — follow these strictly:"]
    for rule in rules:
        label = rule.rule_type.replace("_", " ").upper()
        lines.append(f"{label}: {rule.description}")
    return "\n".join(lines)


def _maybe_append_name_ask(response_text: str, soul: Soul) -> str:
    """On the first interaction, if name still unknown, append a natural name question.

    Only fires on the very first message (interaction_count == 0, before the post-
    response increment). Only if Tier 1 didn't catch a name. Only if the response
    doesn't already contain a name question.
    """
    if soul.interaction_count != 0 or soul.user_name:
        return response_text
    name_question_signals = ["your name", "call you", "who am i talking", "what should i call"]
    if any(signal in response_text.lower() for signal in name_question_signals):
        return response_text
    return response_text.rstrip() + "\n\nBy the way — what should I call you?"


def _is_soul_mature(soul: Soul) -> bool:
    """Check whether the soul has enough substance for bootstrap graduation.

    All four signals must be present — interaction count alone is never sufficient.
    """
    return (
        bool(soul.user_name)
        and bool(soul.user_context)
        and bool(soul.communication_style)
        and soul.interaction_count >= _BOOTSTRAP_MIN_INTERACTIONS
    )


def _build_system_prompt(
    message: NormalizedMessage,
    capability_prompt: str,
    soul: Soul,
    template: AgentTemplate,
    contract_rules: list[CovenantRule],
) -> str:
    """Build a template-driven, soul-aware system prompt.

    Layers (in injection order):
    1. Operating principles — universal KERNOS values
    2. Agent identity / personality — who the agent is for this user
    3. User knowledge — what the agent knows about this person
    4. Platform context — communication channel constraints
    5. Auth context — sender trust level
    6. Behavioral contracts — what the agent must/must-not do
    7. Capabilities — what tools are available
    8. Bootstrap prompt — ONLY if soul has not graduated (bootstrap_graduated == False)
    """
    parts: list[str] = []

    # 1. Operating principles
    parts.append(template.operating_principles)

    # 2. Agent identity / personality
    agent_name = soul.agent_name or "Kernos"
    personality = soul.personality_notes if soul.personality_notes else template.default_personality
    parts.append(
        f"YOUR IDENTITY:\nYou are {agent_name}.\n{personality}"
    )

    # 3. User knowledge (only if the soul has accumulated something)
    user_knowledge_parts: list[str] = []
    if soul.user_name:
        user_knowledge_parts.append(f"User's name: {soul.user_name}")
    if soul.user_context:
        user_knowledge_parts.append(soul.user_context)
    if soul.communication_style:
        user_knowledge_parts.append(f"Communication style: {soul.communication_style}")
    if user_knowledge_parts:
        parts.append("USER CONTEXT:\n" + "\n".join(user_knowledge_parts))

    # 4. Platform context
    platform_line = _PLATFORM_CONTEXT.get(
        message.platform,
        f"You are communicating via {message.platform}. Keep responses concise.",
    )
    parts.append(platform_line)

    # 5. Auth context
    auth_line = _AUTH_CONTEXT.get(
        message.sender_auth_level.value,
        f"Sender auth level: {message.sender_auth_level.value}.",
    )
    parts.append(auth_line)

    # 6. Behavioral contracts
    contracts_text = _format_contracts(contract_rules)
    if contracts_text:
        parts.append(contracts_text)

    # 7. Capabilities
    parts.append(capability_prompt)

    # 8. Bootstrap prompt — only while the soul hasn't graduated
    if not soul.bootstrap_graduated:
        parts.append(template.bootstrap_prompt)

    return "\n\n".join(parts)


class MessageHandler:
    """Receives NormalizedMessages, delegates reasoning to ReasoningService, returns response strings.

    The handler manages message flow: provisioning, history, event bookends (received/sent),
    and persistence. Reasoning — including the tool-use loop — lives in ReasoningService.
    Capability context comes from CapabilityRegistry. Identity comes from the Soul + Template.
    """

    def __init__(
        self,
        mcp: MCPClientManager,
        conversations: ConversationStore,
        tenants: TenantStore,
        audit: AuditStore,
        events: EventStream,
        state: StateStore,
        reasoning: ReasoningService,
        registry: CapabilityRegistry,
        engine: TaskEngine,
    ) -> None:
        self.mcp = mcp
        self.conversations = conversations
        self.tenants = tenants
        self.audit = audit
        self.events = events
        self.state = state
        self.reasoning = reasoning
        self.registry = registry
        self.engine = engine

    async def _ensure_tenant_state(
        self, tenant_id: str, message: NormalizedMessage
    ) -> None:
        """Create or update StateStore profile for this tenant.

        New tenants: create full profile, seed default contract rules.
        Existing tenants: update capabilities field to reflect current registry state.
        """
        profile = await self.state.get_tenant_profile(tenant_id)
        cap_map = {cap.name: cap.status.value for cap in self.registry.get_all()}

        if profile is not None:
            # Always sync capabilities so the profile reflects current registry state
            profile.capabilities = cap_map
            await self.state.save_tenant_profile(tenant_id, profile)
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
            capabilities=cap_map,
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

    async def _get_or_init_soul(self, tenant_id: str) -> Soul:
        """Load the soul for this tenant, or initialize a new unhatched one.

        The soul is saved immediately on creation so it persists even if
        the subsequent reasoning call fails. Also ensures a default daily
        context space exists for the tenant.
        """
        import uuid
        soul = await self.state.get_soul(tenant_id)
        if soul is None:
            soul = Soul(tenant_id=tenant_id)
            await self.state.save_soul(soul)
            logger.info("Initialized new soul for tenant: %s", tenant_id)

        # Ensure a daily context space exists — idempotent
        spaces = await self.state.list_context_spaces(tenant_id)
        if not any(s.is_default for s in spaces):
            now = _now_iso()
            daily_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name="Daily",
                description="General conversation and daily life",
                space_type="daily",
                status="active",
                is_default=True,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(daily_space)
            logger.info("Created default daily context space for tenant: %s", tenant_id)

        return soul

    async def _consolidate_bootstrap(self, soul: Soul) -> None:
        """One-time consolidation: bootstrap wisdom → soul personality notes.

        Uses complete_simple() — stateless, no tools, no task events.
        Graduation is unconditional: if this call fails, soul still graduates.
        """
        from kernos.kernel.template import PRIMARY_TEMPLATE

        prompt = (
            "You are reflecting on your first interactions with a user.\n\n"
            f"Bootstrap intent:\n{PRIMARY_TEMPLATE.bootstrap_prompt}\n\n"
            f"What you've learned:\n"
            f"- Name: {soul.user_name or 'unknown'}\n"
            f"- Context: {soul.user_context or 'unknown'}\n"
            f"- Style: {soul.communication_style or 'unknown'}\n"
            f"- Interactions: {soul.interaction_count}\n\n"
            "Write 2-3 sentences of personality notes — how you'll approach "
            "this person, what matters to them, what tone fits. Be specific. "
            "Don't repeat facts already captured above. Write for the agent, "
            "not the user."
        )
        try:
            notes = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are writing internal notes for an AI agent about their "
                    "relationship with a specific user."
                ),
                user_content=prompt,
                max_tokens=200,
            )
            soul.personality_notes = notes.strip()
        except Exception as exc:
            logger.warning(
                "Bootstrap consolidation failed for %s: %s — graduating without consolidation",
                soul.tenant_id,
                exc,
            )

    async def _post_response_soul_update(self, soul: Soul) -> None:
        """Update the soul after a successful response.

        - If not yet hatched: mark hatched, emit agent.hatched
        - Increment interaction_count
        - Check bootstrap graduation maturity
        - Save
        """
        now = _now_iso()

        if not soul.hatched:
            soul.hatched = True
            soul.hatched_at = now
            try:
                await emit_event(
                    self.events,
                    EventType.AGENT_HATCHED,
                    soul.tenant_id,
                    "handler",
                    payload={
                        "tenant_id": soul.tenant_id,
                        "hatched_at": now,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit agent.hatched: %s", exc)
            logger.info("Soul hatched for tenant: %s", soul.tenant_id)

        soul.interaction_count += 1

        # Check bootstrap graduation: consolidate, then graduate
        if not soul.bootstrap_graduated and _is_soul_mature(soul):
            await self._consolidate_bootstrap(soul)
            soul.bootstrap_graduated = True
            soul.bootstrap_graduated_at = now
            try:
                await emit_event(
                    self.events,
                    EventType.AGENT_BOOTSTRAP_GRADUATED,
                    soul.tenant_id,
                    "handler",
                    payload={
                        "tenant_id": soul.tenant_id,
                        "interaction_count": soul.interaction_count,
                        "graduated_at": now,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit agent.bootstrap_graduated: %s", exc)
            logger.info(
                "Soul bootstrap graduated for tenant: %s (interactions: %d)",
                soul.tenant_id,
                soul.interaction_count,
            )

        await self.state.save_soul(soul)

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
        2. Load soul (initialize if new tenant's first message)
        3. Load recent conversation history
        4. Emit message.received
        5. Store user message (before reasoning call)
        6. Build ReasoningRequest from template + soul + contracts + registry
        7. Delegate to TaskEngine
        8. On success: update soul, store response, emit message.sent, update summary
        """
        tenant_id = derive_tenant_id(message)
        conversation_id = message.conversation_id

        # Steps 1–3: provision, load soul, load history
        await self.tenants.get_or_create(tenant_id)
        await self._ensure_tenant_state(tenant_id, message)
        soul = await self._get_or_init_soul(tenant_id)
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

        # Store user message before the reasoning call
        user_entry = {
            "role": "user",
            "content": message.content,
            "timestamp": message.timestamp.isoformat(),
            "platform": message.platform,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        }
        await self.conversations.append(tenant_id, conversation_id, user_entry)

        # Build and execute task
        task = Task(
            id=generate_task_id(),
            type=TaskType.REACTIVE_SIMPLE,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            source="user_message",
            input_text=message.content,
            created_at=_now_iso(),
        )

        tools = self.registry.get_connected_tools()
        capability_prompt = self.registry.build_capability_prompt()
        contract_rules = await self.state.get_contract_rules(tenant_id, active_only=True)
        system_prompt = _build_system_prompt(
            message, capability_prompt, soul, PRIMARY_TEMPLATE, contract_rules
        )
        messages: list[dict] = history + [{"role": "user", "content": message.content}]

        request = ReasoningRequest(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            model=_MODEL,
            trigger="user_message",
        )

        try:
            task = await self.engine.execute(task, request)
            response_text = task.result_text

            # Tier 1 runs sync (updates soul.user_name / communication_style if found).
            # Tier 2 fires as a background task (does not block the response).
            await run_projectors(
                user_message=message.content,
                recent_turns=history[-4:],
                soul=soul,
                state=self.state,
                events=self.events,
                reasoning_service=self.reasoning,
                tenant_id=tenant_id,
            )

            # Append name ask on first interaction if name still unknown after Tier 1
            response_text = _maybe_append_name_ask(response_text, soul)

        except (ReasoningTimeoutError, ReasoningConnectionError) as exc:
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

        except ReasoningRateLimitError as exc:
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

        except ReasoningProviderError as exc:
            logger.error(
                "Claude API provider error for sender=%s: %s",
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

        # Update soul after successful response
        await self._post_response_soul_update(soul)

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
        try:
            await emit_event(
                self.events,
                EventType.MESSAGE_SENT,
                tenant_id,
                "handler",
                payload={
                    "content": response_text,
                    "conversation_id": conversation_id,
                    "platform": message.platform,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit message.sent: %s", exc)

        await self._update_conversation_summary(tenant_id, conversation_id, message.platform)

        return response_text
