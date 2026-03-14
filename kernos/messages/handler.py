import logging
import os
from datetime import datetime, timezone

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.credentials import resolve_anthropic_credential
from kernos.kernel.engine import TaskEngine
from kernos.kernel.router import LLMRouter, RouterResult
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

SPACE_THREAD_TOKEN_BUDGET = 4000
CROSS_DOMAIN_INJECTION_TURNS = 5
SPACE_CREATION_THRESHOLD = 15
ACTIVE_SPACE_CAP = 40

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


def _is_soul_mature(soul: Soul, *, has_user_knowledge: bool = False) -> bool:
    """Check whether the soul has enough substance for bootstrap graduation.

    All four signals must be present — interaction count alone is never sufficient.
    has_user_knowledge replaces the deprecated soul.user_context check —
    True when the tenant has at least one active user-subject KnowledgeEntry.
    """
    return (
        bool(soul.user_name)
        and has_user_knowledge
        and bool(soul.communication_style)
        and soul.interaction_count >= _BOOTSTRAP_MIN_INTERACTIONS
    )


def _build_system_prompt(
    message: NormalizedMessage,
    capability_prompt: str,
    soul: Soul,
    template: AgentTemplate,
    contract_rules: list[CovenantRule],
    active_space: ContextSpace | None = None,
    cross_domain_prefix: str | None = None,
    user_knowledge_entries: list | None = None,
) -> str:
    """Build a template-driven, soul-aware system prompt.

    Layers (in injection order):
    0. Cross-domain injection — background awareness from other spaces (if any)
    1. Operating principles — universal KERNOS values
    2. Agent identity / personality — who the agent is for this user
    2b. Context space posture — working style override for non-daily spaces
    3. User knowledge — what the agent knows about this person
    4. Platform context — communication channel constraints
    5. Auth context — sender trust level
    6. Behavioral contracts — what the agent must/must-not do
    7. Capabilities — what tools are available
    8. Bootstrap prompt — ONLY if soul has not graduated (bootstrap_graduated == False)
    """
    parts: list[str] = []

    # 0. Compaction context — index, cross-domain injections, compaction document
    # The prefix is built by _assemble_space_context with section headers already applied.
    if cross_domain_prefix:
        parts.append(cross_domain_prefix)

    # 1. Operating principles
    parts.append(template.operating_principles)

    # 2. Agent identity / personality
    agent_name = soul.agent_name or "Kernos"
    personality = soul.personality_notes if soul.personality_notes else template.default_personality
    parts.append(
        f"YOUR IDENTITY:\nYou are {agent_name}.\n{personality}"
    )

    # 2b. Context space posture
    if active_space and not active_space.is_default and active_space.posture:
        parts.append(
            f"## Current operating context: {active_space.name}\n"
            f"(This shapes your working style — it does not override "
            f"your core values or hard boundaries.)\n"
            f"{active_space.posture}"
        )

    # 3. User knowledge — from soul fields + KnowledgeEntries
    user_knowledge_parts: list[str] = []
    if soul.user_name:
        user_knowledge_parts.append(f"User's name: {soul.user_name}")
    if user_knowledge_entries:
        for entry in user_knowledge_entries:
            user_knowledge_parts.append(entry.content)
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
        self._router = LLMRouter(self.state, self.reasoning)

        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import AnthropicTokenAdapter
        self.compaction = CompactionService(
            state=state,
            reasoning=reasoning,
            token_adapter=AnthropicTokenAdapter(resolve_anthropic_credential()),
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            events=events,
        )

        # Wire up file service for kernel file tools
        from kernos.kernel.files import FileService
        self._files = FileService(os.getenv("KERNOS_DATA_DIR", "./data"))
        reasoning.set_files(self._files)
        self.compaction.set_files(self._files)

        # Wire up retrieval service for the `remember` kernel tool
        self._retrieval = None
        try:
            voyage_api_key = os.getenv("VOYAGE_API_KEY", "")
            if voyage_api_key:
                from kernos.kernel.embeddings import EmbeddingService
                from kernos.kernel.embedding_store import JsonEmbeddingStore
                from kernos.kernel.retrieval import RetrievalService
                data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                self._retrieval = RetrievalService(
                    state=state,
                    embedding_service=EmbeddingService(voyage_api_key),
                    embedding_store=JsonEmbeddingStore(data_dir),
                    compaction=self.compaction,
                    reasoning=reasoning,
                )
                reasoning.set_retrieval(self._retrieval)
        except Exception as exc:
            logger.warning("Failed to initialize RetrievalService: %s", exc)

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

            # Initialize compaction state for daily space with default headroom
            try:
                from kernos.kernel.compaction import (
                    CompactionState,
                    compute_document_budget,
                    MODEL_MAX_TOKENS,
                    COMPACTION_MODEL_USABLE_TOKENS,
                    COMPACTION_INSTRUCTION_TOKENS,
                    DEFAULT_DAILY_HEADROOM,
                )
                context_def = (
                    f"Space: {daily_space.name}\nType: {daily_space.space_type}\n"
                    f"Description: {daily_space.description}\nPosture: {daily_space.posture}\n"
                )
                context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
                system_overhead = 4000  # Approximate for daily space
                doc_budget = compute_document_budget(
                    MODEL_MAX_TOKENS, system_overhead, 0, DEFAULT_DAILY_HEADROOM
                )
                daily_comp = CompactionState(
                    space_id=daily_space.id,
                    conversation_headroom=DEFAULT_DAILY_HEADROOM,
                    document_budget=doc_budget,
                    message_ceiling=min(
                        doc_budget,
                        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - context_def_tokens,
                    ),
                    _context_def_tokens=context_def_tokens,
                    _system_overhead=system_overhead,
                )
                await self.compaction.save_state(tenant_id, daily_space.id, daily_comp)
            except Exception as exc:
                logger.warning("Failed to init compaction state for daily space: %s", exc)

        return soul

    async def _consolidate_bootstrap(self, soul: Soul) -> None:
        """One-time consolidation: bootstrap wisdom → soul personality notes.

        Uses complete_simple() — stateless, no tools, no task events.
        Graduation is unconditional: if this call fails, soul still graduates.
        """
        from kernos.kernel.template import PRIMARY_TEMPLATE

        # Query user knowledge from KnowledgeEntries
        user_ke = await self.state.query_knowledge(
            soul.tenant_id, subject="user", active_only=True, limit=20,
        )
        user_facts = [e.content for e in user_ke
                      if e.lifecycle_archetype in ("structural", "identity", "habitual")]
        context_text = "\n".join(f"- {f}" for f in user_facts) if user_facts else "unknown"

        prompt = (
            "You are reflecting on your first interactions with a user.\n\n"
            f"Bootstrap intent:\n{PRIMARY_TEMPLATE.bootstrap_prompt}\n\n"
            f"What you've learned:\n"
            f"- Name: {soul.user_name or 'unknown'}\n"
            f"- Known facts:\n{context_text}\n"
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
        user_ke = await self.state.query_knowledge(
            soul.tenant_id, subject="user", active_only=True, limit=1,
        )
        has_user_knowledge = len(user_ke) > 0
        if not soul.bootstrap_graduated and _is_soul_mature(soul, has_user_knowledge=has_user_knowledge):
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

    def _truncate_to_budget(self, messages: list[dict], budget_tokens: int) -> list[dict]:
        """Drop oldest messages to fit within token budget. 4 chars ≈ 1 token."""
        msgs = list(messages)
        total = sum(len(m.get("content", "")) // 4 for m in msgs)
        while total > budget_tokens and len(msgs) > 2:
            dropped = msgs.pop(0)
            total -= len(dropped.get("content", "")) // 4
        return msgs

    async def _assemble_space_context(
        self,
        tenant_id: str,
        conversation_id: str,
        active_space_id: str,
        active_space: ContextSpace | None,
    ) -> tuple[list[dict], str | None]:
        """Assemble the agent's conversation context for the active space.

        Context window layout (top to bottom):
        [System prompt]                    <- primacy zone
        [Compaction index] (if exists)     <- historical awareness
        [Cross-domain injections]          <- background, low attention
        [Compaction document]
          |-- Ledger (oldest -> newest)    <- middle zone (archival)
          |-- Living State                 <- approaching recency zone
        [Recent conversation messages]     <- strongest recency zone

        Returns (recent_messages, system_prefix) where:
        - recent_messages: messages since last compaction (the live thread)
        - system_prefix: index + cross-domain + compaction doc for system prompt
        """
        prefix_parts: list[str] = []

        # 1. Compaction index (if archives exist)
        comp_state = await self.compaction.load_state(tenant_id, active_space_id)
        if comp_state and comp_state.index_tokens > 0:
            index_text = await self.compaction.load_index(tenant_id, active_space_id)
            if index_text:
                prefix_parts.append(
                    f"## Archived history (summaries — full archives available on request):\n"
                    f"{index_text}"
                )

        # 2. Cross-domain injections — last 5 turns from other spaces
        cross = await self.conversations.get_cross_domain_messages(
            tenant_id, conversation_id, active_space_id,
            last_n_turns=CROSS_DOMAIN_INJECTION_TURNS,
        )
        if cross:
            lines = []
            for msg in cross:
                role_label = "You" if msg["role"] == "assistant" else "User"
                ts = msg.get("timestamp", "")
                content = str(msg.get("content", ""))[:300]
                lines.append(f"[{role_label}, {ts}]: {content}")
            prefix_parts.append(
                f"## Recent activity in other areas (background — read but do not dwell on):\n"
                + "\n".join(lines)
            )

        # 3. Compaction document (Ledger -> Living State)
        active_doc = await self.compaction.load_document(tenant_id, active_space_id)
        if active_doc:
            prefix_parts.append(
                f"## Context history for this space:\n{active_doc}"
            )

        system_prefix = "\n\n".join(prefix_parts) if prefix_parts else None

        # 4. Recent messages since last compaction (the live thread)
        is_daily = active_space.is_default if active_space else False
        thread = await self.conversations.get_space_thread(
            tenant_id, conversation_id, active_space_id,
            max_messages=50,
            include_untagged=is_daily,
            include_timestamp=True,  # Needed for post-compaction filtering
        )

        # Filter to messages since last compaction
        if comp_state and comp_state.last_compaction_at:
            thread = [
                m for m in thread
                if m.get("timestamp", "") > comp_state.last_compaction_at
            ]

        # Strip timestamps before sending to reasoning (only role+content in messages array)
        recent_messages = [
            {"role": m["role"], "content": m["content"]} for m in thread
        ]

        # Fallback: if no compaction state and no compaction doc, use old truncation
        if not comp_state and not active_doc:
            recent_messages = self._truncate_to_budget(recent_messages, SPACE_THREAD_TOKEN_BUDGET)

        return recent_messages, system_prefix

    async def _handle_file_upload(
        self,
        tenant_id: str,
        active_space_id: str,
        filename: str,
        content: str,
    ) -> str:
        """Handle a user-uploaded text file.

        Same storage as agent-created files. Same read_file() interface.
        Returns a notification string to prepend to the user's message context.
        """
        try:
            content.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return "I can only handle text files right now — images and PDFs are coming soon."

        description = f"Uploaded by user on {_now_iso()[:10]}"
        await self._files.write_file(
            tenant_id, active_space_id, filename, content, description
        )
        return f"[File uploaded: {filename}. You can read it with read_file if needed.]"

    async def _run_session_exit(
        self, tenant_id: str, space_id: str, conversation_id: str
    ) -> None:
        """Update space name/description based on what happened in this session."""
        space = await self.state.get_context_space(tenant_id, space_id)
        if not space or space.is_default:
            return

        # Get messages tagged to this space from the conversation
        session_messages = await self.conversations.get_space_thread(
            tenant_id, conversation_id, space_id, max_messages=30
        )
        if len(session_messages) < 3:
            return  # Too short to update description

        formatted = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Agent'}: {str(m.get('content', ''))[:200]}"
            for m in session_messages[-20:]
        )

        EXIT_SCHEMA = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["name", "description"],
            "additionalProperties": False
        }

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Review this conversation session and update the space name and description. "
                    "The description helps the router understand what this space is about. "
                    "Rename the space if the session revealed something the name misses. "
                    "Keep description to 1-3 sentences. Be specific and concrete."
                ),
                user_content=(
                    f"Space: {space.name}\n"
                    f"Current description: {space.description}\n\n"
                    f"Session:\n{formatted}"
                ),
                output_schema=EXIT_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = __import__("json").loads(result_str)
            updates: dict = {}
            if parsed.get("name") and parsed["name"] != space.name:
                updates["name"] = parsed["name"]
            if parsed.get("description") and parsed["description"] != space.description:
                updates["description"] = parsed["description"]
            if updates:
                await self.state.update_context_space(tenant_id, space_id, updates)
                logger.info("Session exit updated space %s: %s", space_id, updates)
        except Exception as exc:
            logger.warning("Session exit maintenance failed for %s: %s", space_id, exc)

    async def _enforce_space_cap(self, tenant_id: str) -> None:
        """Archive the least recently used space if at the active cap."""
        spaces = await self.state.list_context_spaces(tenant_id)
        active = [s for s in spaces if s.status == "active" and not s.is_default]
        if len(active) < ACTIVE_SPACE_CAP:
            return
        lru = sorted(active, key=lambda s: s.last_active_at)[0]
        await self.state.update_context_space(tenant_id, lru.id, {"status": "archived"})
        try:
            await emit_event(
                self.events,
                EventType.CONTEXT_SPACE_SUSPENDED,
                tenant_id,
                "space_cap",
                payload={"space_id": lru.id, "name": lru.name, "reason": "lru_sunset"},
            )
        except Exception as exc:
            logger.warning("Failed to emit context.space.suspended: %s", exc)
        logger.info("Archived LRU space %s (%s) for tenant %s", lru.id, lru.name, tenant_id)

    async def _trigger_gate2(
        self, tenant_id: str, topic_hint: str, conversation_id: str
    ) -> None:
        """Gate 2: LLM decides whether to create a space for this topic cluster."""
        import uuid
        import json as _json

        # Gather sample messages tagged with this hint
        recent = await self.conversations.get_recent_full(tenant_id, conversation_id, limit=100)
        hint_messages = [
            m for m in recent
            if topic_hint in m.get("space_tags", [])
        ]
        if not hint_messages:
            return

        formatted = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Agent'}: {str(m.get('content', ''))[:200]}"
            for m in hint_messages[-20:]
        )

        GATE2_SCHEMA = {
            "type": "object",
            "properties": {
                "create_space": {"type": "boolean"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "reasoning": {"type": "string"}
            },
            "required": ["create_space", "name", "description", "reasoning"],
            "additionalProperties": False
        }

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are evaluating whether a recurring topic in someone's life deserves "
                    "its own dedicated context space. A space is for recurring domains — "
                    "ongoing projects, hobbies, professional areas — not one-off topics that "
                    "happened to run long. If this is a real domain, name it concisely and "
                    "write a 1-2 sentence description of what it covers."
                ),
                user_content=(
                    f"Topic hint: {topic_hint}\n\n"
                    f"Messages about this topic:\n{formatted}"
                ),
                output_schema=GATE2_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)
            if parsed.get("create_space"):
                await self._enforce_space_cap(tenant_id)
                now = _now_iso()
                new_space = ContextSpace(
                    id=f"space_{uuid.uuid4().hex[:8]}",
                    tenant_id=tenant_id,
                    name=parsed.get("name", topic_hint.replace("_", " ").title()),
                    description=parsed.get("description", ""),
                    space_type="domain",
                    status="active",
                    created_at=now,
                    last_active_at=now,
                    is_default=False,
                )
                await self.state.save_context_space(new_space)
                await self.state.clear_topic_hint(tenant_id, topic_hint)

                # Initialize compaction state for the new space
                try:
                    from kernos.kernel.compaction import (
                        CompactionState,
                        compute_document_budget,
                        estimate_headroom,
                        MODEL_MAX_TOKENS,
                        COMPACTION_MODEL_USABLE_TOKENS,
                        COMPACTION_INSTRUCTION_TOKENS,
                    )
                    headroom = await estimate_headroom(self.reasoning, new_space)
                    context_def = (
                        f"Space: {new_space.name}\nType: {new_space.space_type}\n"
                        f"Description: {new_space.description}\nPosture: {new_space.posture}\n"
                    )
                    context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
                    system_overhead = 4000  # Approximate
                    doc_budget = compute_document_budget(
                        MODEL_MAX_TOKENS, system_overhead, 0, headroom
                    )
                    gate2_comp = CompactionState(
                        space_id=new_space.id,
                        conversation_headroom=headroom,
                        document_budget=doc_budget,
                        message_ceiling=min(
                            doc_budget,
                            COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - context_def_tokens,
                        ),
                        _context_def_tokens=context_def_tokens,
                        _system_overhead=system_overhead,
                    )
                    await self.compaction.save_state(tenant_id, new_space.id, gate2_comp)
                except Exception as exc:
                    logger.warning("Failed to init compaction state for gate2 space: %s", exc)

                try:
                    await emit_event(
                        self.events,
                        EventType.CONTEXT_SPACE_CREATED,
                        tenant_id,
                        "gate2",
                        payload={
                            "space_id": new_space.id,
                            "name": new_space.name,
                            "description": new_space.description,
                            "topic_hint": topic_hint,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit context.space.created: %s", exc)
                logger.info(
                    "Gate 2 created space %s (%s) for tenant %s",
                    new_space.id, new_space.name, tenant_id
                )
            else:
                # Not a real domain — clear hint count to avoid re-triggering soon
                await self.state.clear_topic_hint(tenant_id, topic_hint)
                logger.info(
                    "Gate 2 declined space creation for hint '%s' (tenant %s): %s",
                    topic_hint, tenant_id, parsed.get("reasoning", "")
                )
        except Exception as exc:
            logger.warning("Gate 2 failed for hint '%s': %s", topic_hint, exc)

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

        Flow (v2):
        1. Provision + soul init
        2. Load recent history with full metadata
        3. LLM router → RouterResult (tags, focus, continuation)
        4. Detect space switch + session exit on outgoing space
        5. Update last_active_space_id, emit switch event
        6. Gate 1: topic hint tracking for emerging topics
        7. Load active space, update last_active_at
        8. Assemble space-specific context thread + cross-domain prefix
        9. Build system prompt with posture + scoped rules + cross-domain prefix
        10. Reasoning (space thread, not flat history)
        11. Store user + assistant messages with space_tags
        12. Memory projectors, soul update, conversation summary
        """
        tenant_id = derive_tenant_id(message)
        conversation_id = message.conversation_id

        # Steps 1–2: provision, load soul
        await self.tenants.get_or_create(tenant_id)
        await self._ensure_tenant_state(tenant_id, message)
        soul = await self._get_or_init_soul(tenant_id)

        # Step 2: Load recent history with full metadata (for router)
        recent_full = await self.conversations.get_recent_full(
            tenant_id, conversation_id, limit=20
        )

        # Step 3: Route the message (LLM call, or immediate fallback for single-space)
        tenant_profile = await self.state.get_tenant_profile(tenant_id)
        current_focus_id = tenant_profile.last_active_space_id if tenant_profile else ""

        router_result = await self._router.route(
            tenant_id, message.content, recent_full, current_focus_id
        )
        active_space_id = router_result.focus

        # Step 4: Detect space switch
        previous_space_id = current_focus_id
        space_switched = (
            active_space_id != previous_space_id
            and previous_space_id != ""
            and active_space_id != ""
        )

        # Session exit maintenance on the outgoing space (async, best-effort)
        if space_switched:
            import asyncio
            asyncio.create_task(
                self._run_session_exit(tenant_id, previous_space_id, conversation_id)
            )

        # Step 5: Update last_active_space_id + emit switch event
        if tenant_profile and active_space_id and active_space_id != previous_space_id:
            tenant_profile.last_active_space_id = active_space_id
            await self.state.save_tenant_profile(tenant_id, tenant_profile)

        if space_switched:
            try:
                await emit_event(
                    self.events,
                    EventType.CONTEXT_SPACE_SWITCHED,
                    tenant_id,
                    "router",
                    payload={
                        "from_space": previous_space_id,
                        "to_space": active_space_id,
                        "router_tags": router_result.tags,
                        "continuation": router_result.continuation,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit context.space.switched: %s", exc)

        # Step 6: Gate 1 — topic hint tracking for tags that are not known space IDs
        known_space_ids = {
            s.id for s in await self.state.list_context_spaces(tenant_id)
        }
        for tag in router_result.tags:
            if tag and tag not in known_space_ids:
                try:
                    await self.state.increment_topic_hint(tenant_id, tag)
                    count = await self.state.get_topic_hint_count(tenant_id, tag)
                    if count >= SPACE_CREATION_THRESHOLD:
                        import asyncio
                        asyncio.create_task(
                            self._trigger_gate2(tenant_id, tag, conversation_id)
                        )
                except Exception as exc:
                    logger.warning("Gate 1 tracking failed for hint '%s': %s", tag, exc)

        # Step 7: Load active space, update last_active_at
        active_space = (
            await self.state.get_context_space(tenant_id, active_space_id)
            if active_space_id
            else None
        )
        if active_space and active_space_id:
            await self.state.update_context_space(
                tenant_id, active_space_id,
                {"last_active_at": _now_iso(), "status": "active"},
            )

        # Step 7b: Handle file uploads from context (downloaded by platform adapter)
        upload_notifications: list[str] = []
        if message.context and active_space_id:
            for att in message.context.get("attachments", []):
                filename = att.get("filename", "upload.txt")
                content = att.get("content", "")
                note = await self._handle_file_upload(
                    tenant_id, active_space_id, filename, content
                )
                upload_notifications.append(note)

        # Step 8: Assemble space-specific context
        space_messages, cross_domain_prefix = await self._assemble_space_context(
            tenant_id, conversation_id, active_space_id, active_space
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

        # Store user message WITH space_tags
        user_entry = {
            "role": "user",
            "content": message.content,
            "timestamp": message.timestamp.isoformat(),
            "platform": message.platform,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "space_tags": router_result.tags,
        }
        await self.conversations.append(tenant_id, conversation_id, user_entry)

        # Step 9: Build system prompt
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
        # Add the kernel-managed `remember` tool when retrieval is available
        if self._retrieval:
            from kernos.kernel.retrieval import REMEMBER_TOOL
            tools = tools + [REMEMBER_TOOL]
        # Add kernel-managed file tools (always available)
        from kernos.kernel.files import FILE_TOOLS
        tools = tools + FILE_TOOLS
        capability_prompt = self.registry.build_capability_prompt()
        space_scope = [active_space_id, None] if active_space_id else None
        contract_rules = await self.state.query_covenant_rules(
            tenant_id,
            context_space_scope=space_scope,
            active_only=True,
        )
        # Query user knowledge from KnowledgeEntries (replaces soul.user_context)
        user_ke = await self.state.query_knowledge(
            tenant_id, subject="user", active_only=True, limit=50,
        )
        user_knowledge_entries = [
            e for e in user_ke
            if e.lifecycle_archetype in ("structural", "identity", "habitual")
        ]
        system_prompt = _build_system_prompt(
            message, capability_prompt, soul, PRIMARY_TEMPLATE, contract_rules,
            active_space=active_space,
            cross_domain_prefix=cross_domain_prefix,
            user_knowledge_entries=user_knowledge_entries,
        )

        # Step 10: Build messages array from space thread + current user message
        # Prepend upload notifications so the agent knows files arrived
        user_content = message.content
        if upload_notifications:
            user_content = "\n".join(upload_notifications) + ("\n\n" + message.content if message.content else "")
        messages: list[dict] = space_messages + [{"role": "user", "content": user_content}]

        request = ReasoningRequest(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            model=_MODEL,
            trigger="user_message",
            active_space_id=active_space_id,
            input_text=message.content,
        )

        try:
            task = await self.engine.execute(task, request)
            response_text = task.result_text

            # Tier 1 + Tier 2 projectors
            history = await self.conversations.get_recent(
                tenant_id, conversation_id, limit=20
            )
            await run_projectors(
                user_message=message.content,
                recent_turns=history[-4:],
                soul=soul,
                state=self.state,
                events=self.events,
                reasoning_service=self.reasoning,
                tenant_id=tenant_id,
                active_space_id=active_space_id,
                active_space=active_space,
            )

            response_text = _maybe_append_name_ask(response_text, soul)

        except (ReasoningTimeoutError, ReasoningConnectionError) as exc:
            logger.error(
                "Claude API connection/timeout error for sender=%s: %s",
                message.sender, exc, exc_info=True,
            )
            try:
                await emit_event(
                    self.events, EventType.HANDLER_ERROR, tenant_id, "handler",
                    payload={"error_type": type(exc).__name__, "error_message": str(exc),
                             "conversation_id": conversation_id, "stage": "api_call"},
                )
            except Exception:
                pass
            return "Something went wrong on my end — try again in a moment."

        except ReasoningRateLimitError as exc:
            logger.error(
                "Claude API rate limit hit for sender=%s: %s",
                message.sender, exc, exc_info=True,
            )
            try:
                await emit_event(
                    self.events, EventType.HANDLER_ERROR, tenant_id, "handler",
                    payload={"error_type": type(exc).__name__, "error_message": str(exc),
                             "conversation_id": conversation_id, "stage": "api_call"},
                )
            except Exception:
                pass
            return "I'm a bit overloaded right now. Try again in a minute."

        except ReasoningProviderError as exc:
            logger.error(
                "Claude API provider error for sender=%s: %s",
                message.sender, exc, exc_info=True,
            )
            try:
                await emit_event(
                    self.events, EventType.HANDLER_ERROR, tenant_id, "handler",
                    payload={"error_type": type(exc).__name__, "error_message": str(exc),
                             "conversation_id": conversation_id, "stage": "api_call"},
                )
            except Exception:
                pass
            return "Something went wrong on my end — try again in a moment."

        except Exception as exc:
            logger.error(
                "Unexpected error in handler for sender=%s: %s",
                message.sender, exc, exc_info=True,
            )
            try:
                await emit_event(
                    self.events, EventType.HANDLER_ERROR, tenant_id, "handler",
                    payload={"error_type": type(exc).__name__, "error_message": str(exc),
                             "conversation_id": conversation_id, "stage": "general"},
                )
            except Exception:
                pass
            return "Something unexpected happened. Try again, and if it keeps happening, let me know."

        # Update soul after successful response
        await self._post_response_soul_update(soul)

        # Store assistant response WITH space_tags
        assistant_entry = {
            "role": "assistant",
            "content": response_text,
            "timestamp": _now_iso(),
            "platform": message.platform,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "space_tags": router_result.tags,
        }
        await self.conversations.append(tenant_id, conversation_id, assistant_entry)

        # Track tokens for compaction trigger
        try:
            comp_state = await self.compaction.load_state(tenant_id, active_space_id)
            if comp_state:
                exchange_tokens = await self.compaction.adapter.count_tokens(
                    message.content + "\n" + response_text
                )
                comp_state.cumulative_new_tokens += exchange_tokens

                if await self.compaction.should_compact(active_space_id, comp_state):
                    # Get messages with timestamps for compaction
                    is_daily = active_space.is_default if active_space else False
                    space_thread_full = await self.conversations.get_space_thread(
                        tenant_id, conversation_id, active_space_id,
                        max_messages=200,
                        include_untagged=is_daily,
                        include_timestamp=True,
                    )
                    # Filter to messages since last compaction
                    new_messages = [
                        m for m in space_thread_full
                        if m.get("timestamp", "") > (comp_state.last_compaction_at or "")
                    ]
                    if new_messages and active_space:
                        comp_state = await self.compaction.compact(
                            tenant_id, active_space_id, active_space,
                            new_messages, comp_state,
                        )
                else:
                    await self.compaction.save_state(tenant_id, active_space_id, comp_state)
        except Exception as exc:
            logger.warning("Compaction tracking failed for %s/%s: %s", tenant_id, active_space_id, exc)

        # Emit message.sent
        try:
            await emit_event(
                self.events, EventType.MESSAGE_SENT, tenant_id, "handler",
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
