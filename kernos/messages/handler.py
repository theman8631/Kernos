import asyncio
import json
import time
from kernos.utils import utc_now
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from kernos.kernel.reasoning import PendingAction, ReasoningRequest, ReasoningService
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

_MAX_ERROR_BUFFER = 20


class ErrorBuffer:
    """Collects WARNING/ERROR log entries for developer mode error surfacing.

    Per-tenant buffer. Only captures kernos.* loggers. Ephemeral — in-memory only.
    """

    def __init__(self) -> None:
        self._entries: dict[str, list[str]] = {}
        self._dropped: dict[str, int] = {}
        self._handler = _ErrorBufferLogHandler(self)
        # Attach to the kernos root logger
        kernos_logger = logging.getLogger("kernos")
        kernos_logger.addHandler(self._handler)
        self._current_tenant_id: str = ""

    def set_tenant(self, tenant_id: str) -> None:
        """Set which tenant is currently being processed."""
        self._current_tenant_id = tenant_id
        self._handler._current_tenant_id = tenant_id

    def collect(self, tenant_id: str, entry: str) -> None:
        """Add an error entry to the buffer."""
        entries = self._entries.setdefault(tenant_id, [])
        if len(entries) >= _MAX_ERROR_BUFFER:
            self._dropped[tenant_id] = self._dropped.get(tenant_id, 0) + 1
        else:
            entries.append(entry)

    def drain(self, tenant_id: str) -> str:
        """Pop all pending errors for a tenant, formatted as a block. Returns '' if none."""
        entries = self._entries.pop(tenant_id, [])
        dropped = self._dropped.pop(tenant_id, 0)
        if not entries:
            return ""
        lines = ["[DEVELOPER: Errors since last message]"]
        if dropped:
            lines.append(f"({dropped} earlier errors omitted)")
        lines.extend(entries)
        lines.append(
            "\nThese are internal system errors visible because developer mode is enabled. "
            "You can discuss them, diagnose them (read_doc or read_source), or ignore them."
        )
        lines.append("[END DEVELOPER]")
        return "\n".join(lines)


class _ErrorBufferLogHandler(logging.Handler):
    """Logging handler that feeds WARNING+ entries into ErrorBuffer."""

    def __init__(self, buffer: ErrorBuffer) -> None:
        super().__init__(level=logging.WARNING)
        self._buffer = buffer
        self._current_tenant_id: str = ""

    def emit(self, record: logging.LogRecord) -> None:
        if self._current_tenant_id and record.name.startswith("kernos."):
            ts = self.format(record) if self.formatter else record.getMessage()
            entry = f"{record.levelname} {record.name}: {record.getMessage()}"
            self._buffer.collect(self._current_tenant_id, entry)


@dataclass
class TurnContext:
    """Accumulated state across the six processing phases."""

    # Phase 1: Provision
    tenant_id: str = ""
    conversation_id: str = ""
    member_id: str = ""
    soul: Soul | None = None
    message: NormalizedMessage | None = None

    # Phase 2: Route
    active_space_id: str = ""
    active_space: ContextSpace | None = None
    router_result: RouterResult | None = None
    previous_space_id: str = ""
    space_switched: bool = False
    upload_notifications: list[str] = field(default_factory=list)

    # Phase 3: Assemble
    system_prompt: str = ""
    system_prompt_static: str = ""   # Cacheable prefix (RULES + ACTIONS)
    system_prompt_dynamic: str = ""  # Fresh each turn (NOW + STATE + RESULTS + MEMORY)
    tools: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    results_prefix: str | None = None
    memory_prefix: str | None = None
    merged_count: int = 0  # Number of user messages merged into this turn

    # Phase 4: Reason
    response_text: str = ""
    task: Task | None = None

    # Post-turn trace (for friction observer)
    tool_calls_trace: list[dict] = field(default_factory=list)  # [{name, input, success}]
    pref_detected: bool = False  # Whether preference parser detected a preference this turn

    # Phase timing (ms) — populated by process() and _run_space_loop
    phase_timings: dict[str, int] = field(default_factory=dict)


# Turn serialization: per-(tenant, space) mailbox/runner
MERGE_WINDOW_MS = 300  # Wait up to 300ms for follow-up messages


@dataclass
class SpaceRunner:
    """Per-(tenant, space) turn runner with mailbox."""

    tenant_id: str
    space_id: str
    mailbox: asyncio.Queue  # (NormalizedMessage, TurnContext, asyncio.Future) items
    _task: asyncio.Task | None = field(default=None, repr=False)
    provider_errors: list[str] = field(default_factory=list)  # Session-level error accumulator


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
        "a few sentences max. No one wants a wall of text on their phone. "
        "If content is long (reports, detailed explanations, lists), "
        "offer to send it to Discord instead using send_to_channel. "
        "Use abbreviations where natural."
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




_SECURE_API_TRIGGER = "secure api"
_SECURE_INPUT_TIMEOUT_MINUTES = 10


@dataclass
class SecureInputState:
    """Per-tenant state for secure credential input mode."""
    capability_name: str
    expires_at: datetime


def _safe_tenant_name(tenant_id: str) -> str:
    """Make tenant_id safe for filesystem use."""
    return re.sub(r"[^\w.-]", "_", tenant_id)


def resolve_mcp_credentials(
    server_config: dict,
    tenant_id: str,
    secrets_dir: str,
) -> dict[str, str]:
    """Resolve credential references to actual values for MCP server env.

    Reads the .key file from secrets/, injects into env_template.
    Falls back to environment variable with same name if no key file found.
    """
    credentials_key = server_config.get("credentials_key", "")
    env_template = server_config.get("env_template", {})
    resolved: dict[str, str] = {}

    credential_value = ""
    if credentials_key:
        secret_path = (
            Path(secrets_dir) / _safe_tenant_name(tenant_id) / f"{credentials_key}.key"
        )
        if secret_path.exists():
            credential_value = secret_path.read_text().strip()

    for key, template in env_template.items():
        if "{credentials}" in template:
            if credential_value:
                resolved[key] = template.replace("{credentials}", credential_value)
            else:
                resolved[key] = os.getenv(key, "")
        else:
            resolved[key] = template

    return resolved


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


# Category → tool name mapping for dynamic tool surfacing (V1 policy)
CATEGORY_TOOLS: dict[str, set[str]] = {
    "calendar": {
        "list-events", "search-events", "create-event",
        "update-event", "delete-event", "get-event",
        "get-freebusy", "get-current-time", "list-calendars",
        "list-colors", "respond-to-event", "manage-accounts",
        "create-events", "manage_schedule",
    },
    "search": {"brave_web_search", "brave_local_search"},
    "browser": {
        "goto", "markdown", "links", "evaluate",
        "semantic_tree", "interactiveElements", "structuredData",
    },
    "messaging": {"send_to_channel", "manage_channels"},
    "identity": {"read_soul", "update_soul"},
    "source": {"read_source"},
    "files": {"write_file", "read_file", "list_files", "delete_file"},
    "covenants": {"manage_covenants"},
}

_CATEGORY_SIGNALS: dict[str, list[str]] = {
    "calendar": ["calendar", "event", "schedule", "appointment", "meeting",
                 "remind me", "tomorrow", "today", "next week",
                 "this week", "free time", "busy", "block off"],
    "search": ["search", "look up", "find", "what is", "who is",
               "how to", "research", "google", "near me", "nearby",
               "places", "in my area", "where can i", "restaurants",
               "hot dog", "ice cream", "pizza"],
    "browser": ["website", "page", "browse", "http", "www", ".com", ".org"],
    "messaging": ["text me", "a text", "sms", "send a message", "notify",
                  "tell them", "send to channel", "discord"],
    "identity": ["personality", "who are you", "your name", "identity", "soul"],
    "source": ["code", "source", "implementation", "debug", "read_source"],
    "files": ["write file", "read file", "save file", "draft", "my notes",
              "my files", "list files", "delete file"],
    "covenants": ["rule", "covenant", "never", "always", "don't ever"],
}


def _select_tool_categories(message_text: str, recent_topic: str) -> set[str]:
    """Select tool categories based on turn context. Pure string matching — no LLM."""
    combined = f"{message_text.lower()} {recent_topic.lower()}"
    categories: set[str] = set()
    for cat, signals in _CATEGORY_SIGNALS.items():
        if any(sig in combined for sig in signals):
            categories.add(cat)
    return categories


def _is_stale_knowledge(entry, days: int = 14) -> bool:
    """Check if a knowledge entry's last_referenced is older than N days."""
    ref = getattr(entry, "last_referenced", "") or ""
    if not ref:
        return False
    try:
        from kernos.utils import utc_now_dt
        ref_dt = datetime.fromisoformat(ref)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        return (utc_now_dt() - ref_dt).days > days
    except (ValueError, TypeError):
        return False


def _build_rules_block(
    template: AgentTemplate, contract_rules: list[CovenantRule], soul: Soul,
) -> str:
    """## RULES — operating principles + behavioral contracts + bootstrap."""
    parts = [template.operating_principles]
    contracts_text = _format_contracts(contract_rules)
    if contracts_text:
        parts.append(contracts_text)
    if not soul.bootstrap_graduated:
        parts.append(template.bootstrap_prompt)
    return "## RULES\n" + "\n\n".join(parts)


def _build_now_block(
    message: NormalizedMessage, soul: Soul,
    active_space: ContextSpace | None,
) -> str:
    """## NOW — turn-local operating situation: time, platform, auth, space."""
    from kernos.utils import utc_now_dt, format_user_datetime
    now_utc = utc_now_dt()
    user_tz = soul.timezone or ""
    tz_display = user_tz or "system local"
    date_line = (
        f"Current time: {format_user_datetime(now_utc, user_tz)} "
        f"({tz_display}) / "
        f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    platform_line = _PLATFORM_CONTEXT.get(
        message.platform,
        f"You are communicating via {message.platform}. Keep responses concise.",
    )
    auth_line = _AUTH_CONTEXT.get(
        message.sender_auth_level.value,
        f"Sender auth level: {message.sender_auth_level.value}.",
    )
    parts = [date_line, platform_line, auth_line]
    if active_space and not active_space.is_default and active_space.posture:
        parts.append(
            f"Current operating context: {active_space.name}\n"
            f"(This shapes your working style — it does not override "
            f"your core values or hard boundaries.)\n"
            f"{active_space.posture}"
        )
    return "## NOW\n" + "\n".join(parts)


def _build_state_block(
    soul: Soul, template: AgentTemplate,
    user_knowledge_entries: list | None,
) -> str:
    """## STATE — current truth the agent should act from."""
    agent_name = soul.agent_name or "Kernos"
    personality = soul.personality_notes if soul.personality_notes else template.default_personality
    parts = [f"Identity: {agent_name}\n{personality}"]
    user_parts: list[str] = []
    if soul.user_name:
        user_parts.append(f"User's name: {soul.user_name}")
    if user_knowledge_entries:
        for entry in user_knowledge_entries:
            user_parts.append(entry.content)
    if soul.communication_style:
        user_parts.append(f"Communication style: {soul.communication_style}")
    if user_parts:
        parts.append("USER CONTEXT:\n" + "\n".join(user_parts))
    return "## STATE\n" + "\n\n".join(parts)


def _build_results_block(results_prefix: str | None) -> str:
    """## RESULTS — receipts, system events, awareness whispers, pending notices."""
    parts: list[str] = []
    if results_prefix:
        parts.append(results_prefix)
    if not parts:
        return ""
    return "## RESULTS\n" + "\n\n".join(parts)


def _build_actions_block(
    capability_prompt: str, message: NormalizedMessage,
    channel_registry: "ChannelRegistry | None",
) -> str:
    """## ACTIONS — capabilities, outbound channels, docs."""
    from kernos.messages.reference import DOCS_HINT
    parts = [capability_prompt]
    connected = channel_registry.get_connected() if channel_registry else []
    if connected:
        channel_lines = []
        for ch in connected:
            marker = " (current)" if ch.platform == message.platform else ""
            outbound = "can send" if ch.can_send_outbound else "receive only"
            channel_lines.append(
                f"- {ch.name}: {ch.display_name} [{outbound}]{marker}"
            )
        parts.append(
            "OUTBOUND CHANNELS (use send_to_channel to deliver to a "
            "specific channel):\n" + "\n".join(channel_lines)
        )
    parts.append(DOCS_HINT)
    parts.append(
        "TOOL AVAILABILITY: Your current tool set is filtered to match this "
        "turn's context. Additional tools from connected services are available "
        "— use request_tool to load a specific tool if needed."
    )
    return "## ACTIONS\n" + "\n\n".join(parts)


def _build_memory_block(memory_prefix: str | None) -> str:
    """## MEMORY — compaction context (Living State, archived history index)."""
    parts: list[str] = []
    if memory_prefix:
        parts.append(memory_prefix)
    if not parts:
        return ""
    return "## MEMORY\n" + "\n\n".join(parts)


def _compose_blocks(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


def _build_system_prompt(
    message: NormalizedMessage,
    capability_prompt: str,
    soul: Soul,
    template: AgentTemplate,
    contract_rules: list[CovenantRule],
    active_space: ContextSpace | None = None,
    cross_domain_prefix: str | None = None,
    user_knowledge_entries: list | None = None,
    channel_registry: "ChannelRegistry | None" = None,
) -> str:
    """Compatibility wrapper — assembles Cognitive UI blocks.

    Maintained for tests that call _build_system_prompt directly.
    Production code uses the phase-based block builders.
    """
    rules = _build_rules_block(template, contract_rules, soul)
    now_block = _build_now_block(message, soul, active_space)
    state_block = _build_state_block(soul, template, user_knowledge_entries)
    results = _build_results_block(cross_domain_prefix)
    actions = _build_actions_block(capability_prompt, message, channel_registry)
    memory = _build_memory_block(cross_domain_prefix)  # compat: uses same prefix
    # Block order: static prefix (RULES, ACTIONS) then dynamic (NOW, STATE, RESULTS, MEMORY)
    return _compose_blocks(rules, actions, now_block, state_block, results, memory)


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
        secrets_dir: str = "",
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
        self._secrets_dir = secrets_dir or os.getenv("KERNOS_SECRETS_DIR", "./secrets")
        self._secure_input_state: dict[str, SecureInputState] = {}
        self._mcp_config_loaded: set[str] = set()
        self._covenant_cleanup_done: set[str] = set()
        self._evaluators: dict[str, "AwarenessEvaluator"] = {}  # per-tenant evaluators
        self._error_buffer = ErrorBuffer()
        self._pending_system_events: dict[str, list[str]] = {}
        self._compacting: set[str] = set()  # space_ids currently compacting
        self.preference_parsing_enabled: bool = True  # Bypassable (Agent Card principle)
        self._runners: dict[str, SpaceRunner] = {}  # "tenant:space" → SpaceRunner
        self._adapters: dict[str, "BaseAdapter"] = {}  # platform → adapter
        from kernos.kernel.channels import ChannelRegistry
        self._channel_registry = ChannelRegistry()
        reasoning.set_channel_registry(self._channel_registry)

        from kernos.kernel.scheduler import TriggerStore
        self._trigger_store = TriggerStore(os.getenv("KERNOS_DATA_DIR", "./data"))
        reasoning.set_trigger_store(self._trigger_store)
        reasoning.set_handler(self)

        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        self.compaction = CompactionService(
            state=state,
            reasoning=reasoning,
            token_adapter=EstimateTokenAdapter(),
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            events=events,
        )

        # Per-space conversation log (P1 — write-only, parallel to existing store)
        from kernos.kernel.conversation_log import ConversationLogger
        self.conv_logger = ConversationLogger(data_dir=os.getenv("KERNOS_DATA_DIR", "./data"))

        # Wire up file service for kernel file tools
        from kernos.kernel.files import FileService
        self._files = FileService(os.getenv("KERNOS_DATA_DIR", "./data"))
        reasoning.set_files(self._files)
        self.compaction.set_files(self._files)
        reasoning.set_registry(registry)
        reasoning.set_state(state)

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

        # Phase timing accumulator for /status averages
        self._phase_timing_history: list[dict[str, int]] = []  # list of {phase: ms} dicts

        # Friction observer — post-turn diagnostics
        from kernos.kernel.friction import FrictionObserver
        self._friction = FrictionObserver(
            reasoning=reasoning,
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            enabled=os.getenv("KERNOS_FRICTION_OBSERVER", "1") != "0",
        )

    async def _get_system_space(self, tenant_id: str):
        """Return the system context space for this tenant, or None."""
        try:
            spaces = await self.state.list_context_spaces(tenant_id)
            for space in spaces:
                if space.space_type == "system":
                    return space
        except Exception:
            pass
        return None

    async def _write_capabilities_overview(
        self, tenant_id: str, system_space_id: str
    ) -> None:
        """Write capabilities-overview.md to the system space — called after install/uninstall."""
        if not getattr(self, "_files", None):
            return
        connected = self.registry.get_connected()
        available = self.registry.get_available()

        content = "# Connected Tools\n\n"
        if connected:
            for cap in connected:
                universal_tag = " (available everywhere)" if cap.universal else ""
                content += f"- **{cap.name}**{universal_tag}: {cap.description}\n"
                if cap.tools:
                    content += f"  Tools: {', '.join(cap.tools)}\n"
        else:
            content += "No tools connected yet.\n"

        content += "\n# Available to Connect\n\n"
        if available:
            for cap in available:
                content += f"- **{cap.name}**: {cap.description}\n"
        else:
            content += "No additional tools available.\n"

        try:
            await self._files.write_file(
                tenant_id, system_space_id,
                "capabilities-overview.md", content,
                "What tools are connected and available — updated on changes",
            )
        except Exception as exc:
            logger.warning("Failed to write capabilities-overview.md: %s", exc)

    async def _infer_pending_capability(
        self, tenant_id: str, conversation_id: str
    ) -> str | None:
        """Infer which capability is being set up from recent system space messages.

        Scans the last 5 messages in the system space for capability name mentions.
        Returns the capability name if found, None otherwise.
        """
        system_space = await self._get_system_space(tenant_id)
        if not system_space:
            return None

        try:
            recent = await self.conversations.get_space_thread(
                tenant_id, conversation_id, system_space.id, max_messages=5
            )
        except Exception:
            return None

        available = self.registry.get_available()
        for cap in available:
            for msg in recent:
                content = str(msg.get("content", "")).lower()
                if cap.name.lower() in content or cap.display_name.lower() in content:
                    return cap.name

        return None

    async def _store_credential(
        self, tenant_id: str, capability_name: str, value: str
    ) -> None:
        """Store a credential in the secrets directory with restrictive permissions.

        Secrets live OUTSIDE the data directory and are never readable by agents.
        """
        secrets_dir = Path(self._secrets_dir) / _safe_tenant_name(tenant_id)
        secrets_dir.mkdir(parents=True, exist_ok=True)
        secret_path = secrets_dir / f"{capability_name}.key"
        secret_path.write_text(value.strip())
        secret_path.chmod(0o600)
        logger.info("Stored credential for %s/%s", tenant_id, capability_name)

    async def _connect_after_credential(
        self, tenant_id: str, capability_name: str
    ) -> bool:
        """Connect an MCP server after credentials have been stored."""
        from mcp import StdioServerParameters
        from kernos.capability.registry import CapabilityStatus

        cap = self.registry.get(capability_name)
        if not cap:
            return False

        resolved_env = resolve_mcp_credentials(
            {"credentials_key": cap.credentials_key, "env_template": cap.env_template},
            tenant_id,
            self._secrets_dir,
        )
        params = StdioServerParameters(
            command=cap.server_command,
            args=list(cap.server_args),
            env=resolved_env,
        )
        self.mcp.register_server(capability_name, params)

        # Register auth command if the capability defines one
        if cap.auth_args:
            from kernos.capability.client import AuthCommand
            self.mcp.register_auth_command(
                capability_name,
                AuthCommand(
                    command=cap.server_command,
                    args=list(cap.auth_args),
                    env=resolved_env,
                    probe_tool=cap.auth_probe_tool,
                ),
            )

        success = await self.mcp.connect_one(capability_name)

        if success:
            tools = self.mcp.get_tool_definitions().get(capability_name, [])
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]

            await self._persist_mcp_config(tenant_id)

            system_space = await self._get_system_space(tenant_id)
            if system_space:
                await self._write_capabilities_overview(tenant_id, system_space.id)

            try:
                await emit_event(
                    self.events, EventType.TOOL_INSTALLED, tenant_id, "mcp_installer",
                    payload={
                        "capability_name": capability_name,
                        "tool_count": len(cap.tools),
                        "universal": cap.universal,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit tool.installed: %s", exc)

        return success

    async def _persist_mcp_config(self, tenant_id: str) -> None:
        """Write current MCP config to mcp-servers.json in the system space."""
        from kernos.capability.registry import CapabilityStatus

        system_space = await self._get_system_space(tenant_id)
        if not system_space or not getattr(self, "_files", None):
            return

        config: dict = {"servers": {}, "uninstalled": [], "disabled": []}
        for cap in self.registry.get_all():
            if cap.status in (CapabilityStatus.CONNECTED, CapabilityStatus.DISABLED) and cap.server_name:
                config["servers"][cap.name] = {
                    "display_name": cap.display_name,
                    "command": cap.server_command,
                    "args": list(cap.server_args),
                    "credentials_key": cap.credentials_key,
                    "env_template": dict(cap.env_template),
                    "universal": cap.universal,
                    "tool_effects": dict(cap.tool_effects),
                    "source": cap.source,
                }
            if cap.status == CapabilityStatus.SUPPRESSED:
                config["uninstalled"].append(cap.name)
            elif cap.status == CapabilityStatus.DISABLED:
                config["disabled"].append(cap.name)

        try:
            await self._files.write_file(
                tenant_id, system_space.id,
                "mcp-servers.json",
                json.dumps(config, indent=2),
                "MCP server configurations — managed by the system",
            )
        except Exception as exc:
            logger.warning("Failed to persist mcp config for %s: %s", tenant_id, exc)

    async def _disconnect_capability(
        self, tenant_id: str, capability_name: str
    ) -> bool:
        """Disconnect an MCP server and update all state."""
        from kernos.capability.registry import CapabilityStatus

        success = await self.mcp.disconnect_one(capability_name)
        if success:
            cap = self.registry.get(capability_name)
            if cap:
                cap.status = CapabilityStatus.SUPPRESSED
                cap.tools = []

            await self._persist_mcp_config(tenant_id)

            system_space = await self._get_system_space(tenant_id)
            if system_space:
                await self._write_capabilities_overview(tenant_id, system_space.id)

            try:
                await emit_event(
                    self.events, EventType.TOOL_UNINSTALLED, tenant_id, "mcp_installer",
                    payload={"capability_name": capability_name},
                )
            except Exception as exc:
                logger.warning("Failed to emit tool.uninstalled: %s", exc)

        return success

    async def _maybe_start_evaluator(self, tenant_id: str) -> None:
        """Start an AwarenessEvaluator for this tenant (once per process per tenant).

        The evaluator runs two phases:
        - Awareness pass (whispers from foresight signals) — every 1800s
        - Trigger evaluation (scheduled actions) — every 60s
        """
        if tenant_id in self._evaluators:
            return
        try:
            from kernos.kernel.awareness import AwarenessEvaluator
            evaluator = AwarenessEvaluator(
                state=self.state,
                events=self.events,
                interval_seconds=int(os.getenv("KERNOS_AWARENESS_INTERVAL", "1800")),
                trigger_interval_seconds=int(os.getenv("KERNOS_TRIGGER_INTERVAL", "15")),
                trigger_store=self._trigger_store,
                handler=self,
            )
            await evaluator.start(tenant_id)
            self._evaluators[tenant_id] = evaluator
        except Exception as exc:
            logger.warning("Failed to start AwarenessEvaluator for %s: %s", tenant_id, exc)

    def register_adapter(self, platform: str, adapter: "BaseAdapter") -> None:
        """Register a platform adapter for outbound messaging."""
        from kernos.kernel.channels import ChannelInfo
        self._adapters[platform] = adapter

    def register_channel(
        self, name: str, display_name: str, platform: str,
        can_send_outbound: bool, channel_target: str = "",
        status: str = "connected", source: str = "default",
    ) -> None:
        """Register a communication channel in the channel registry."""
        from kernos.kernel.channels import ChannelInfo
        self._channel_registry.register(ChannelInfo(
            name=name,
            display_name=display_name,
            status=status,
            source=source,
            can_send_outbound=can_send_outbound,
            channel_target=channel_target,
            platform=platform,
        ))

    def _resolve_member(self, tenant_id: str, platform: str, sender: str) -> str:
        """Resolve a sender identity signal to a member_id.

        For now: single member per instance = owner.
        Future: lookup in members table by identity signal.
        """
        from kernos.kernel.scheduler import resolve_owner_member_id
        return resolve_owner_member_id(tenant_id)

    async def read_log_text(self, tenant_id: str, space_id: str, log_number: int) -> str:
        """Read conversation log text — satisfies HandlerProtocol."""
        result = await self.conv_logger.read_log_text(tenant_id, space_id, log_number)
        return result or ""

    def queue_system_event(self, tenant_id: str, event: str) -> None:
        """Queue a system event for injection into the next system prompt."""
        self._pending_system_events.setdefault(tenant_id, []).append(event)
        logger.info("SYSTEM_EVENT_QUEUED: tenant=%s event=%s", tenant_id, event[:100])

    def drain_system_events(self, tenant_id: str) -> list[str]:
        """Drain and return all pending system events for a tenant."""
        return self._pending_system_events.pop(tenant_id, [])

    async def send_outbound(
        self, tenant_id: str, member_id: str,
        channel_name: str | None, message: str,
    ) -> bool:
        """Send an unprompted message to the user on a specific or default channel.

        Returns True if sent, False on failure.
        """
        from kernos.kernel.channels import ChannelInfo

        if channel_name:
            ch = self._channel_registry.get(channel_name)
        else:
            # Pick most recently used outbound-capable channel
            capable = self._channel_registry.get_outbound_capable()
            ch = capable[0] if capable else None

        if not ch:
            logger.warning(
                "OUTBOUND: no channel available tenant=%s member=%s channel=%s",
                tenant_id, member_id, channel_name,
            )
            return False

        if ch.status != "connected":
            logger.warning(
                "OUTBOUND: channel=%s not connected (status=%s)",
                ch.name, ch.status,
            )
            return False

        adapter = self._adapters.get(ch.platform)
        if not adapter:
            logger.warning("OUTBOUND: no adapter for platform=%s", ch.platform)
            return False

        success = await adapter.send_outbound(tenant_id, ch.channel_target, message)
        logger.info(
            "OUTBOUND: channel=%s target=%s tenant=%s member=%s length=%d success=%s",
            ch.name, ch.channel_target, tenant_id, member_id, len(message), success,
        )
        return success

    async def _maybe_run_covenant_cleanup(self, tenant_id: str) -> None:
        """Run one-time covenant dedup/contradiction cleanup per tenant per process."""
        if tenant_id in self._covenant_cleanup_done:
            return
        self._covenant_cleanup_done.add(tenant_id)

        try:
            from kernos.kernel.covenant_manager import run_covenant_cleanup
            embedding_service = None
            if self._retrieval:
                embedding_service = getattr(self._retrieval, '_embedding_service', None)
            stats = await run_covenant_cleanup(
                self.state, tenant_id,
                embedding_service=embedding_service,
            )
            if stats["deduped"] or stats["contradictions_resolved"]:
                logger.info(
                    "COVENANT_CLEANUP: tenant=%s deduped=%d contradictions=%d",
                    tenant_id, stats["deduped"], stats["contradictions_resolved"],
                )
        except Exception as exc:
            logger.warning("Covenant cleanup failed for %s: %s", tenant_id, exc)

    async def _maybe_load_mcp_config(self, tenant_id: str) -> None:
        """Load persisted MCP config for this tenant (once per process lifetime per tenant).

        Called after soul/space init so the system space is guaranteed to exist.
        Suppresses uninstalled entries and connects any persisted servers.
        """
        from kernos.capability.registry import CapabilityStatus
        from mcp import StdioServerParameters

        if tenant_id in self._mcp_config_loaded:
            return
        self._mcp_config_loaded.add(tenant_id)

        system_space = await self._get_system_space(tenant_id)
        if not system_space or not getattr(self, "_files", None):
            return

        try:
            config_raw = await self._files.read_file(
                tenant_id, system_space.id, "mcp-servers.json"
            )
            if not config_raw or config_raw.startswith("Error:"):
                return
            config = json.loads(config_raw)
        except Exception as exc:
            logger.warning("Failed to load mcp-servers.json for %s: %s", tenant_id, exc)
            return

        # Suppress uninstalled entries
        for name in config.get("uninstalled", []):
            cap = self.registry.get(name)
            if cap and cap.status != CapabilityStatus.CONNECTED:
                cap.status = CapabilityStatus.SUPPRESSED

        # Restore disabled state for capabilities that are connected but user disabled
        for name in config.get("disabled", []):
            cap = self.registry.get(name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                cap.status = CapabilityStatus.DISABLED

        # Migration: check for new defaults in known.py not yet in tenant config
        # These appear as "available" — the user can enable them via manage_capabilities
        known_in_config = set(config.get("servers", {}).keys()) | set(config.get("uninstalled", [])) | set(config.get("disabled", []))
        for cap in self.registry.get_all():
            if cap.source == "default" and cap.name not in known_in_config:
                logger.info(
                    "New default capability '%s' available for tenant %s",
                    cap.name, tenant_id,
                )

        # Connect persisted servers not already connected
        for name, server_config in config.get("servers", {}).items():
            cap = self.registry.get(name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                continue  # Already connected at startup
            resolved_env = resolve_mcp_credentials(
                server_config, tenant_id, self._secrets_dir
            )
            self.mcp.register_server(
                name,
                StdioServerParameters(
                    command=server_config.get("command", ""),
                    args=list(server_config.get("args", [])),
                    env=resolved_env,
                ),
            )

            # Register auth command if capability defines one
            if cap and cap.auth_args:
                from kernos.capability.client import AuthCommand
                self.mcp.register_auth_command(
                    name,
                    AuthCommand(
                        command=cap.server_command,
                        args=list(cap.auth_args),
                        env=resolved_env,
                        probe_tool=cap.auth_probe_tool,
                    ),
                )

            success = await self.mcp.connect_one(name)
            if success:
                tools = self.mcp.get_tool_definitions().get(name, [])
                if cap:
                    cap.status = CapabilityStatus.CONNECTED
                    cap.tools = [t["name"] for t in tools]
                    if server_config.get("source"):
                        cap.source = server_config["source"]
                logger.info("Loaded and connected %s from persisted config", name)

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

        now = utc_now()
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

    async def _write_system_docs(
        self, tenant_id: str, system_space_id: str
    ) -> None:
        """Write capabilities-overview.md to the system space.

        Self-knowledge docs (how-i-work.md, kernos-reference.md, how-to-connect-tools.md)
        are deprecated — replaced by docs/ + read_doc() (SPEC-3J).
        Only capabilities-overview.md remains (dynamically updated on install/uninstall).
        """
        if not getattr(self, "_files", None):
            return
        registry = getattr(self, "registry", None)
        if not registry:
            return

        await self._write_capabilities_overview(tenant_id, system_space_id)

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
            await self.state.save_soul(soul, source="soul_init", trigger="new_tenant")
            logger.info("Initialized new soul for tenant: %s", tenant_id)

        # Timezone discovery: infer from system local if not yet set
        if not soul.timezone:
            try:
                _sys_tz = str(datetime.now().astimezone().tzinfo)
                if _sys_tz and "/" in _sys_tz:  # IANA format check
                    soul.timezone = _sys_tz
                    await self.state.save_soul(
                        soul, source="handler_process", trigger="timezone_discovery",
                    )
                    logger.info(
                        "TIMEZONE_DISCOVERED: tenant=%s tz=%s source=system_local",
                        tenant_id, _sys_tz,
                    )
            except Exception:
                pass

        # Ensure default context space exists — idempotent
        spaces = await self.state.list_context_spaces(tenant_id)
        # Migrate existing "Daily" spaces to "General"
        for s in spaces:
            if s.is_default and s.name == "Daily":
                await self.state.update_context_space(tenant_id, s.id, {"name": "General"})
                s.name = "General"
                logger.info("SPACE_MIGRATE: renamed Daily→General for tenant=%s space=%s", tenant_id, s.id)
        if not any(s.is_default for s in spaces):
            now = utc_now()
            daily_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name="General",
                description="General conversation and daily life",
                space_type="general",
                status="active",
                is_default=True,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(daily_space)
            logger.info("Created default General context space for tenant: %s", tenant_id)

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

        # Ensure a system context space exists — idempotent
        spaces_now = await self.state.list_context_spaces(tenant_id)
        if not any(s.space_type == "system" for s in spaces_now):
            now = utc_now()
            system_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name="System",
                description=(
                    "System configuration and management. Install and manage tools, "
                    "view connected capabilities, get help with how the system works."
                ),
                space_type="system",
                status="active",
                posture=(
                    "Precise and careful. Configuration changes affect the whole system. "
                    "Confirm before modifying system settings or tool configurations.\n\n"
                    "TOOL CONNECTION:\n"
                    "You can help users connect and manage their tools. When a user wants "
                    "to connect a new tool:\n"
                    "1. Identify the capability from the known catalog\n"
                    "2. Explain what's needed (API key, account setup, etc.)\n"
                    "3. Walk them through getting the credential\n"
                    "4. For the credential handoff, instruct them: \"When you have your key "
                    "ready, reply with exactly: secure api\"\n"
                    "5. The system handles the rest — you'll be told if it succeeded\n\n"
                    "NEVER ask users to paste API keys directly in conversation.\n"
                    "ALWAYS use the 'secure api' flow for credentials.\n\n"
                    "If a capability requires a web interface (requires_web_interface=True), "
                    "explain that it can't be set up in this channel yet and will be available "
                    "when the web interface ships."
                ),
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(system_space)
            logger.info("Created system context space for tenant: %s", tenant_id)
            # Write documentation files to the system space
            await self._write_system_docs(tenant_id, system_space.id)

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
        now = utc_now()

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

        await self.state.save_soul(soul, source="handler_process", trigger="interaction_count_update")

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
    ) -> tuple[list[dict], str | None, str | None]:
        """Assemble the agent's conversation context for the active space.

        Returns (recent_messages, results_prefix, memory_prefix) where:
        - recent_messages: messages since last compaction (the live thread)
        - results_prefix: receipts, system events, awareness (for ## RESULTS)
        - memory_prefix: compaction index + document (for ## MEMORY)
        """
        results_parts: list[str] = []
        memory_parts: list[str] = []

        # 1. Compaction index → MEMORY
        comp_state = await self.compaction.load_state(tenant_id, active_space_id)
        if comp_state and comp_state.index_tokens > 0:
            index_text = await self.compaction.load_index(tenant_id, active_space_id)
            if index_text:
                memory_parts.append(
                    f"Archived history (summaries — full archives available on request):\n"
                    f"{index_text}"
                )

        # 2. Proactive awareness → RESULTS
        awareness_block = await self._get_pending_awareness(tenant_id, active_space_id)
        if awareness_block:
            results_parts.append(awareness_block)

        # 2b. System events → RESULTS
        system_events = self.drain_system_events(tenant_id)
        if system_events:
            events_block = "RECENT SYSTEM EVENTS:\n" + "\n".join(system_events)
            results_parts.append(events_block)
            logger.info(
                "SYSTEM_EVENTS_INJECTED: tenant=%s count=%d",
                tenant_id, len(system_events),
            )
            for evt in system_events:
                try:
                    await self.conv_logger.append(
                        tenant_id=tenant_id,
                        space_id=active_space_id,
                        speaker="system",
                        channel="internal",
                        content=evt,
                    )
                except Exception:
                    pass

        # 3. Compaction document → MEMORY
        active_doc = await self.compaction.load_context_document(tenant_id, active_space_id)
        if active_doc:
            memory_parts.append(
                f"Context history for this space:\n{active_doc}"
            )

        # 4. Parent briefing → MEMORY (for child domains)
        if active_space and active_space.parent_id:
            try:
                briefing = await self._load_parent_briefing(
                    tenant_id, active_space.parent_id, active_space_id)
                if briefing:
                    parent = await self.state.get_context_space(tenant_id, active_space.parent_id)
                    parent_name = parent.name if parent else "parent"
                    memory_parts.append(
                        f"Briefing from {parent_name} (may be stale — use remember() for current data):\n{briefing}"
                    )
            except Exception as exc:
                logger.warning("BRIEFING_LOAD: failed for space=%s: %s", active_space_id, exc)

        results_prefix = "\n\n".join(results_parts) if results_parts else None
        memory_prefix = "\n\n".join(memory_parts) if memory_parts else None

        # 4. Recent messages — read from space log (P2), fallback to legacy store
        recent_messages: list[dict] = []
        _context_source = "none"
        try:
            log_entries = await self.conv_logger.read_recent(
                tenant_id, active_space_id,
                token_budget=SPACE_THREAD_TOKEN_BUDGET,
                max_messages=50,
            )
            if log_entries:
                recent_messages = [
                    {"role": e["role"], "content": e["content"]}
                    for e in log_entries
                ]
                _context_source = "space_log"
        except Exception as exc:
            logger.warning("CONTEXT_SOURCE: space=%s log_read_failed=%s", active_space_id, exc)

        if not recent_messages:
            # Fallback: no usable log entries — use legacy channel-specific store
            is_daily = active_space.is_default if active_space else False
            thread = await self.conversations.get_space_thread(
                tenant_id, conversation_id, active_space_id,
                max_messages=50,
                include_untagged=is_daily,
                include_timestamp=True,
            )
            if comp_state and comp_state.last_compaction_at:
                thread = [
                    m for m in thread
                    if m.get("timestamp", "") > comp_state.last_compaction_at
                ]
            recent_messages = [
                {"role": m["role"], "content": m["content"]} for m in thread
            ]
            if not comp_state and not active_doc:
                recent_messages = self._truncate_to_budget(recent_messages, SPACE_THREAD_TOKEN_BUDGET)
            _context_source = "legacy_store"

        logger.info(
            "CONTEXT_SOURCE: space=%s source=%s entries=%d",
            active_space_id, _context_source, len(recent_messages),
        )

        # Sanitize: strip messages with empty content (e.g. from a file-only upload that
        # was stored before the empty-message guard was added). The Anthropic API returns
        # 400 on empty content strings.
        sanitized = []
        for m in recent_messages:
            if not m["content"] or not m["content"].strip():
                logger.warning(
                    "EMPTY_MSG_SANITIZE: dropping %s message with empty content from thread",
                    m["role"],
                )
                continue
            sanitized.append(m)
        recent_messages = sanitized

        # Sanitize: merge any trailing user messages (orphaned from rapid-fire or failed request).
        # The Anthropic API requires alternating roles. If consecutive user messages exist,
        # merge them into one so the content isn't lost. The agent sees all user input.
        merged_orphans: list[str] = []
        while recent_messages and recent_messages[-1]["role"] == "user":
            orphan = recent_messages.pop()
            merged_orphans.insert(0, orphan["content"])
            logger.info(
                "ORPHANED_USER_MSG: merging trailing user message into next turn. "
                "Content: %.100s",
                orphan["content"],
            )
        # Orphaned content will be prepended to the current user message in _phase_assemble
        if merged_orphans:
            self._orphaned_user_content = merged_orphans

        return recent_messages, results_prefix, memory_prefix

    async def _get_pending_awareness(self, tenant_id: str, active_space_id: str) -> str:
        """Get pending whispers formatted for the agent's context."""
        from kernos.kernel.awareness import SuppressionEntry

        whispers = await self.state.get_pending_whispers(tenant_id)

        if not whispers:
            return ""

        # Filter to whispers targeting this space or with no space target
        relevant = [
            w for w in whispers
            if w.target_space_id == active_space_id
            or w.target_space_id == ""
            or w.source_space_id == active_space_id
        ]

        if not relevant:
            return ""

        # Sort: stage before ambient
        relevant.sort(key=lambda w: 0 if w.delivery_class == "stage" else 1)

        lines = ["## Proactive awareness (surface naturally — do not dump as a list)"]
        lines.append("")
        lines.append(
            "The following signals were detected since the last conversation. "
            "Weave relevant ones into your response naturally. "
            "If the user asks why you're mentioning something, you can draw "
            "on the reasoning trace."
        )
        lines.append("")

        for w in relevant:
            lines.append(f"- [{w.delivery_class.upper()}] (id: {w.whisper_id}) {w.insight_text}")
            lines.append(f"  Reasoning: {w.reasoning_trace}")
            lines.append("")

        lines.append(
            "If the user says they already know about something or don't want "
            "to hear about it, use dismiss_whisper(whisper_id) to suppress it."
        )

        # Mark as surfaced and create suppression entries
        for w in relevant:
            w.surfaced_at = datetime.now(timezone.utc).isoformat()
            await self.state.mark_whisper_surfaced(tenant_id, w.whisper_id)

            suppression = SuppressionEntry(
                whisper_id=w.whisper_id,
                knowledge_entry_id=w.knowledge_entry_id,
                foresight_signal=w.foresight_signal,
                created_at=w.created_at,
                resolution_state="surfaced",
            )
            await self.state.save_suppression(tenant_id, suppression)

        logger.info("AWARENESS: injected whispers=%d for space=%s",
                     len(relevant), active_space_id)

        return "\n".join(lines)

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

        description = f"Uploaded by user on {utc_now()[:10]}"
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
        active = [s for s in spaces if s.status == "active" and not s.is_default and s.space_type != "system"]
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
        hint_count = len(hint_messages)
        logger.info(
            "SPACE_GATE2: hint=%s count=%d threshold=%d action=evaluating",
            topic_hint, hint_count, SPACE_CREATION_THRESHOLD,
        )
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
                "reasoning": {"type": "string"},
                "recommended_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Capability names from the installed list that are relevant to this space",
                },
            },
            "required": ["create_space", "name", "description", "reasoning", "recommended_tools"],
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
                    f"Messages about this topic:\n{formatted}\n\n"
                    f"Installed tools available for activation:\n"
                    f"{self.registry.get_capability_descriptions()}\n\n"
                    f"Populate recommended_tools with capability names likely to be useful for this space. "
                    f"Use exact capability names from the installed list above."
                ),
                output_schema=GATE2_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)
            if parsed.get("create_space"):
                await self._enforce_space_cap(tenant_id)
                now = utc_now()
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

                # Seed active_tools from Gate 2 recommendations
                installed_names = set(self.registry.get_connected_capability_names())
                recommended = parsed.get("recommended_tools", [])
                seeded_tools = [t for t in recommended if t in installed_names]
                if seeded_tools:
                    new_space.active_tools = seeded_tools
                    await self.state.update_context_space(
                        tenant_id, new_space.id, {"active_tools": seeded_tools}
                    )

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
                    "SPACE_CREATE: id=%s name=%s parent=General reason=%r",
                    new_space.id, new_space.name, parsed.get("reasoning", ""),
                )
            else:
                # Not a real domain — clear hint count to avoid re-triggering soon
                await self.state.clear_topic_hint(tenant_id, topic_hint)
                logger.info(
                    "SPACE_GATE2: hint=%s action=rejected reason=%r",
                    topic_hint, parsed.get("reasoning", ""),
                )
        except Exception as exc:
            logger.warning("Gate 2 failed for hint '%s': %s", topic_hint, exc)

    # --- Domain assessment (CS-2) ---

    DOMAIN_ASSESSMENT_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "create_domain": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "reasoning": {"type": "string"},
        },
        "required": ["create_domain", "confidence", "name", "description", "reasoning"],
        "additionalProperties": False,
    }

    async def _assess_domain_creation(
        self, tenant_id: str, space_id: str, space: ContextSpace, comp_state: "CompactionState",
    ) -> None:
        """Assess whether compacted conversation constitutes a new domain.

        Runs after compaction completes. Only HIGH confidence creates domains.
        """
        import uuid as _uuid
        import json as _json

        # Only assess from general or parent spaces (depth < 2)
        if space.space_type not in ("general", "domain"):
            return
        if space.depth >= 2:
            return

        # Load the freshly compacted document
        doc = await self.compaction.load_document(tenant_id, space_id)
        if not doc:
            return

        # Build existing space list for context
        all_spaces = await self.state.list_context_spaces(tenant_id)
        existing = [
            f"- {s.name} ({s.space_type}, depth={s.depth})"
            for s in all_spaces if s.status == "active" and s.space_type != "system"
        ]

        child_type = "domain" if space.depth == 0 else "subdomain"

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are assessing whether a conversation belongs in its own "
                    f"dedicated context {child_type}, or should remain in the current space.\n\n"
                    "Only create on HIGH confidence. A domain should:\n"
                    "- Have clear internal coherence (not a grab-bag)\n"
                    "- Likely recur in future conversations\n"
                    "- Benefit from isolated context (for BOTH domain AND parent)\n"
                    "- Have a stable, clear label\n\n"
                    "A single conversation about a topic is NOT enough. "
                    "The topic must have depth and likely recurrence.\n"
                    '"D&D Campaign" is a domain. "Random questions" is not.'
                ),
                user_content=(
                    f"Current space: {space.name} (depth={space.depth})\n"
                    f"Existing spaces:\n" + ("\n".join(existing) or "(none)") + "\n\n"
                    f"Compaction summary:\n{doc[:3000]}"
                ),
                output_schema=self.DOMAIN_ASSESSMENT_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)

            if not parsed.get("create_domain"):
                logger.info(
                    "DOMAIN_ASSESS: space=%s result=keep confidence=%s reason=%r",
                    space_id, parsed.get("confidence", "?"), parsed.get("reasoning", ""),
                )
                return

            if parsed.get("confidence") != "high":
                logger.info(
                    "DOMAIN_ASSESS: space=%s result=skip_low_confidence confidence=%s",
                    space_id, parsed.get("confidence", "?"),
                )
                return

            # Check we're not duplicating an existing space
            new_name = parsed.get("name", "").strip()
            if not new_name:
                return
            for s in all_spaces:
                if s.name.lower() == new_name.lower() or new_name.lower() in [a.lower() for a in s.aliases]:
                    logger.info("DOMAIN_ASSESS: space=%s result=duplicate name=%s existing=%s", space_id, new_name, s.id)
                    return

            # Enforce space cap
            await self._enforce_space_cap(tenant_id)

            now = utc_now()
            new_space = ContextSpace(
                id=f"space_{_uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name=new_name,
                description=parsed.get("description", ""),
                space_type=child_type,
                status="active",
                is_default=False,
                parent_id=space_id,
                depth=space.depth + 1,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(new_space)

            # Initialize compaction state with reference-based origin
            try:
                from kernos.kernel.compaction import (
                    CompactionState as _CS,
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
                system_overhead = 4000
                doc_budget = compute_document_budget(
                    MODEL_MAX_TOKENS, system_overhead, 0, headroom
                )
                new_comp = _CS(
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
                await self.compaction.save_state(tenant_id, new_space.id, new_comp)

                # Write reference-based origin document
                origin_doc = (
                    f"## Origin\n"
                    f"This domain originated from {space.name}, "
                    f"compaction #{comp_state.global_compaction_number}.\n"
                    f"Use remember() to retrieve historical context from the parent.\n"
                )
                origin_path = self.compaction._space_dir(tenant_id, new_space.id) / "active_document.md"
                origin_path.parent.mkdir(parents=True, exist_ok=True)
                origin_path.write_text(origin_doc, encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to init compaction for domain %s: %s", new_space.id, exc)

            try:
                from kernos.kernel.event_types import EventType as _ET
                await emit_event(self.events, _ET.CONTEXT_SPACE_CREATED, tenant_id, "domain_assessment",
                    payload={"space_id": new_space.id, "name": new_space.name,
                             "description": new_space.description, "parent_id": space_id,
                             "depth": new_space.depth})
            except Exception:
                pass

            logger.info(
                "DOMAIN_CREATE: space=%s name=%s parent=%s depth=%d confidence=%s",
                new_space.id, new_space.name, space_id, new_space.depth, parsed.get("confidence"),
            )
        except Exception as exc:
            logger.warning("DOMAIN_ASSESS: failed for space=%s: %s", space_id, exc)

    async def _produce_child_briefings(
        self, tenant_id: str, space_id: str, space: ContextSpace,
    ) -> None:
        """Produce context briefings for all child domains after parent compaction."""
        children = await self.state.list_child_spaces(tenant_id, space_id)
        if not children:
            return

        # Load the freshly compacted document (Living State)
        doc = await self.compaction.load_document(tenant_id, space_id)
        if not doc:
            return

        for child in children:
            try:
                briefing = await self.reasoning.complete_simple(
                    system_prompt=(
                        "You are producing a context briefing for a child domain. "
                        "Extract ONLY durable truths relevant to the child domain. "
                        "Keep it short — 3-8 bullet points of facts, decisions, "
                        "and active status. No narrative. No history."
                    ),
                    user_content=(
                        f"Parent: {space.name}\n"
                        f"Child: {child.name} — {child.description}\n\n"
                        f"Parent's current state:\n{doc[:4000]}"
                    ),
                    max_tokens=512,
                    prefer_cheap=True,
                )
                if briefing and briefing.strip():
                    briefing_path = (
                        self.compaction._space_dir(tenant_id, space_id)
                        / f"briefing_{child.id}.md"
                    )
                    briefing_path.parent.mkdir(parents=True, exist_ok=True)
                    briefing_path.write_text(briefing.strip(), encoding="utf-8")
                    logger.info("BRIEFING_PRODUCED: parent=%s child=%s chars=%d",
                        space_id, child.id, len(briefing))
            except Exception as exc:
                logger.warning("BRIEFING_FAILED: parent=%s child=%s error=%s", space_id, child.id, exc)

    async def _load_parent_briefing(
        self, tenant_id: str, parent_id: str, child_id: str,
    ) -> str | None:
        """Load a parent's briefing for a specific child. Returns None if not found."""
        briefing_path = (
            self.compaction._space_dir(tenant_id, parent_id)
            / f"briefing_{child_id}.md"
        )
        if not briefing_path.exists():
            return None
        return briefing_path.read_text(encoding="utf-8")

    async def _update_conversation_summary(
        self, tenant_id: str, conversation_id: str, platform: str
    ) -> None:
        now = utc_now()
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

    # -----------------------------------------------------------------------
    # Turn Serialization — Per-Space Mailbox/Runner
    # -----------------------------------------------------------------------

    def _get_runner(self, tenant_id: str, space_id: str) -> SpaceRunner:
        """Get or create the runner for a (tenant, space) pair."""
        key = f"{tenant_id}:{space_id}"
        if key not in self._runners:
            runner = SpaceRunner(
                tenant_id=tenant_id,
                space_id=space_id,
                mailbox=asyncio.Queue(),
            )
            runner._task = asyncio.create_task(
                self._run_space_loop(runner),
                name=f"runner:{key}",
            )
            self._runners[key] = runner
        return self._runners[key]

    async def _run_space_loop(self, runner: SpaceRunner) -> None:
        """Process turns sequentially for one (tenant, space) pair.

        Pulls messages from the mailbox, merges rapid follow-ups,
        processes one turn at a time, delivers responses.
        """
        while True:
            merged_messages: list[tuple[NormalizedMessage, TurnContext, asyncio.Future]] = []
            try:
                # Block until at least one message arrives
                msg, ctx, future = await runner.mailbox.get()
                merged_messages = [(msg, ctx, future)]

                # Merge window: wait briefly for follow-up messages
                try:
                    await asyncio.sleep(MERGE_WINDOW_MS / 1000)
                except asyncio.CancelledError:
                    raise

                # Drain any additional messages that arrived during the window
                while not runner.mailbox.empty():
                    extra = runner.mailbox.get_nowait()
                    merged_messages.append(extra)

                if len(merged_messages) > 1:
                    logger.info(
                        "TURN_MERGED: space=%s merged=%d",
                        runner.space_id, len(merged_messages),
                    )

                # Process as one turn using the first message's context
                primary_msg, primary_ctx, primary_future = merged_messages[0]
                primary_ctx.merged_count = len(merged_messages)

                # Log merged messages to conversation log so agent sees them
                for extra_msg, extra_ctx, extra_future in merged_messages[1:]:
                    try:
                        await self.conv_logger.append(
                            runner.tenant_id, runner.space_id,
                            speaker="user",
                            channel=extra_msg.platform,
                            content=extra_msg.content,
                        )
                    except Exception as exc:
                        logger.warning("Failed to log merged message: %s", exc)

                # Execute the full turn (assemble → reason → persist)
                _turn_t0 = time.monotonic()
                try:
                    _t0 = time.monotonic()
                    await self._phase_assemble(primary_ctx)
                    primary_ctx.phase_timings["assemble"] = int((time.monotonic() - _t0) * 1000)

                    # Slash command intercepts — skip reasoning
                    _cmd = (primary_msg.content or "").strip()
                    _cmd_lower = _cmd.lower()
                    if _cmd_lower == "/dump":
                        response = await self._handle_dump(primary_ctx)
                    elif _cmd_lower == "/status":
                        response = await self._handle_status(primary_ctx)
                    elif _cmd_lower == "/help":
                        response = self._handle_help()
                    elif _cmd_lower.startswith("/spaces"):
                        response = await self._handle_spaces(primary_ctx, _cmd)
                    else:
                        try:
                            _t0 = time.monotonic()
                            await self._phase_reason(primary_ctx)
                        except (ReasoningTimeoutError, ReasoningConnectionError) as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, "try again in a moment")
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except ReasoningRateLimitError as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, "overloaded right now. Try again in a minute")
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except ReasoningProviderError as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, "try again in a moment")
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except Exception as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, "something unexpected happened")
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)

                        _t0 = time.monotonic()
                        await self._phase_consequence(primary_ctx)
                        primary_ctx.phase_timings["consequence"] = int((time.monotonic() - _t0) * 1000)

                        _t0 = time.monotonic()
                        await self._phase_persist(primary_ctx)
                        primary_ctx.phase_timings["persist"] = int((time.monotonic() - _t0) * 1000)

                        response = primary_ctx.response_text or ""

                        # Friction observer — async, non-blocking
                        primary_ctx.tool_calls_trace = self.reasoning.drain_tool_trace()
                        asyncio.ensure_future(self._run_friction_observer(
                            primary_ctx, provider_errors=runner.provider_errors))
                except Exception as exc:
                    logger.error(
                        "TURN_ERROR: space=%s error=%s",
                        runner.space_id, exc, exc_info=True,
                    )
                    response = "Something went wrong. Try again in a moment."

                # Log phase timings
                _total_ms = int((time.monotonic() - _turn_t0) * 1000)
                _pt = primary_ctx.phase_timings
                for _phase, _dur in _pt.items():
                    logger.info("PHASE_TIMING: phase=%s duration_ms=%d", _phase, _dur)
                logger.info(
                    "TURN_TIMING: total_ms=%d provision=%d route=%d assemble=%d "
                    "reason=%d consequence=%d persist=%d",
                    _total_ms,
                    _pt.get("provision", 0), _pt.get("route", 0),
                    _pt.get("assemble", 0), _pt.get("reason", 0),
                    _pt.get("consequence", 0), _pt.get("persist", 0),
                )
                self._record_phase_timings(_pt, _total_ms)

                # Resolve all futures — primary gets the response,
                # merged messages get empty (adapter sends nothing)
                if not primary_future.done():
                    primary_future.set_result(response)
                for _, _, extra_future in merged_messages[1:]:
                    if not extra_future.done():
                        extra_future.set_result("")

            except asyncio.CancelledError:
                # Resolve any pending futures before exiting
                for item in merged_messages:
                    _, _, f = item
                    if not f.done():
                        f.set_result("")
                break
            except Exception as exc:
                logger.error(
                    "RUNNER_ERROR: space=%s error=%s",
                    runner.space_id, exc, exc_info=True,
                )
                # Resolve any pending futures so callers don't hang
                for item in merged_messages:
                    _, _, f = item
                    if not f.done():
                        f.set_result("Something went wrong. Try again.")

    async def shutdown_runners(self) -> None:
        """Cancel all space runners. Call on application shutdown."""
        for key, runner in list(self._runners.items()):
            if runner._task and not runner._task.done():
                runner._task.cancel()
                try:
                    await runner._task
                except asyncio.CancelledError:
                    pass
        self._runners.clear()

    # -----------------------------------------------------------------------
    # Six-Phase Pipeline (SPEC-HANDLER-DECOMPOSE)
    # -----------------------------------------------------------------------

    async def process(self, message: NormalizedMessage) -> str:
        """Process a NormalizedMessage and return a response string.

        Lightweight phases (provision, route) run immediately. The heavy
        phases (assemble → reason → consequence → persist) are submitted
        to a per-(tenant, space) runner that serializes turns.
        """
        ctx = TurnContext(message=message)

        # Early return paths (secure input)
        early = await self._check_early_return(ctx)
        if early is not None:
            return early

        # Lightweight phases — safe to run concurrently
        _t0 = time.monotonic()
        await self._phase_provision(ctx)
        ctx.phase_timings["provision"] = int((time.monotonic() - _t0) * 1000)

        _t0 = time.monotonic()
        await self._phase_route(ctx)
        ctx.phase_timings["route"] = int((time.monotonic() - _t0) * 1000)

        # Submit to the space runner's mailbox
        runner = self._get_runner(ctx.tenant_id, ctx.active_space_id)

        response_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        await runner.mailbox.put((message, ctx, response_future))

        logger.info(
            "TURN_SUBMITTED: tenant=%s space=%s queue_depth=%d",
            ctx.tenant_id, ctx.active_space_id,
            runner.mailbox.qsize(),
        )

        # Await the response — runner will resolve the future
        return await response_future

    async def _check_early_return(self, ctx: TurnContext) -> str | None:
        """Secure input intercepts — return early without LLM."""
        message = ctx.message
        tenant_id = derive_tenant_id(message)
        conversation_id = message.conversation_id
        ctx.tenant_id = tenant_id
        ctx.conversation_id = conversation_id

        # Housekeeping
        self.reasoning.reset_conflict_raised()
        self.reasoning.cleanup_expired_authorizations(tenant_id)
        self._error_buffer.set_tenant(tenant_id)
        message.member_id = self._resolve_member(tenant_id, message.platform, message.sender)
        if message.platform == "discord":
            self._channel_registry.update_target("discord", message.conversation_id)

        if tenant_id in self._secure_input_state:
            state = self._secure_input_state[tenant_id]
            if datetime.now(timezone.utc) > state.expires_at:
                del self._secure_input_state[tenant_id]
                return (
                    "The secure input session timed out after 10 minutes. "
                    "Your message was processed normally (not stored as a credential). "
                    "Say 'secure api' again when you're ready to send your key."
                )
            credential_value = message.content.strip()
            cap_name = state.capability_name
            del self._secure_input_state[tenant_id]
            await self._store_credential(tenant_id, cap_name, credential_value)
            success = await self._connect_after_credential(tenant_id, cap_name)
            if success:
                return f"Key stored securely. {cap_name} is now connected! You can start using it right away."
            return f"Key stored, but I couldn't connect to {cap_name}. The key might be invalid, or the service might be down."

        if message.content.strip().lower() == _SECURE_API_TRIGGER:
            cap_name = await self._infer_pending_capability(tenant_id, conversation_id)
            if not cap_name:
                return "I'm not sure which tool you're setting up. Head to system settings and start the connection process first."
            self._secure_input_state[tenant_id] = SecureInputState(
                capability_name=cap_name,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=_SECURE_INPUT_TIMEOUT_MINUTES),
            )
            return (
                f"Secure input mode active for {cap_name}. "
                f"Your next message will NOT be seen by any agent — "
                f"it will go directly to encrypted storage as your {cap_name} API key. Send your key now."
            )
        return None

    async def _handle_dump(self, ctx: TurnContext) -> str:
        """Write the fully assembled context to a diagnostic file, skip reasoning."""
        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        # Second-precision timestamp — duplicate deliveries overwrite the same file
        ts = utc_now()[:19].replace(":", "-")
        dump_path = Path(data_dir) / "diagnostics" / f"context_{ts}.txt"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dump_path, "w") as f:
            f.write("=== SYSTEM PROMPT ===\n\n")
            f.write(ctx.system_prompt)
            f.write("\n\n=== MESSAGES ===\n\n")
            for msg in ctx.messages:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, str):
                    f.write(f"[{role}]\n{content}\n\n")
                elif isinstance(content, list):
                    f.write(f"[{role}] <{len(content)} content blocks>\n\n")
                else:
                    f.write(f"[{role}] <non-text content>\n\n")
            f.write("\n=== TOOLS ===\n\n")
            for tool in ctx.tools:
                f.write(f"{json.dumps(tool, indent=2)}\n\n")
            f.write("\n=== SUMMARY ===\n")
            _sys_chars = len(ctx.system_prompt)
            msg_chars = sum(len(str(m.get('content', ''))) for m in ctx.messages)
            tool_chars = sum(len(json.dumps(t)) for t in ctx.tools)
            _char_est = (_sys_chars + msg_chars + tool_chars) // 4
            _real_baseline = self.reasoning.get_last_real_input_tokens(ctx.tenant_id)
            _static_chars = len(ctx.system_prompt_static)
            _dynamic_chars = len(ctx.system_prompt_dynamic)
            f.write(f"System prompt: ~{_sys_chars // 4} tokens ({_sys_chars} chars)\n")
            f.write(f"  Static (cached): ~{_static_chars // 4} tokens ({_static_chars} chars)\n")
            f.write(f"  Dynamic (fresh):  ~{_dynamic_chars // 4} tokens ({_dynamic_chars} chars)\n")
            f.write(f"Messages: {len(ctx.messages)} entries, ~{msg_chars // 4} tokens\n")
            f.write(f"Tools: {len(ctx.tools)} schemas, ~{tool_chars // 4} tokens\n")
            f.write(f"Char-based estimate: ~{_char_est} tokens\n")
            if _real_baseline > 0:
                f.write(f"Last real input_tokens (from API): {_real_baseline}\n")

        logger.info("DUMP: context written to %s", dump_path)
        return f"Context dumped to {dump_path}"

    @staticmethod
    def _handle_help() -> str:
        """Return a summary of available slash commands."""
        return (
            "**Available Commands**\n\n"
            "**/help** — Show this message.\n\n"
            "**/dump** — Write the fully assembled context (system prompt, "
            "messages, tools) to a diagnostic file. Useful for inspecting "
            "exactly what the agent sees on a given turn. Skips reasoning.\n\n"
            "**/status** — Write the operator state view to a diagnostic "
            "file. Shows active preferences, triggers, covenants, key facts, "
            "connected capabilities, legacy artifacts, stale reconciliation, "
            "and degraded services. Skips reasoning.\n\n"
            "**/spaces** — List all context spaces with status.\n"
            '**/spaces create "Name" "Description"** — Manually create a '
            "new context space for testing multi-space routing.\n\n"
            "These commands bypass the reasoning engine and are not stored "
            "in conversation history."
        )

    async def _handle_spaces(self, ctx: TurnContext, raw_cmd: str) -> str:
        """List spaces or create a new one manually."""
        import uuid as _uuid
        import shlex

        tenant_id = ctx.tenant_id
        parts = raw_cmd.strip().split(None, 1)
        sub = parts[1].strip() if len(parts) > 1 else ""

        if sub.lower().startswith("create"):
            # /spaces create "Name" "Description"
            create_args = sub[len("create"):].strip()
            try:
                tokens = shlex.split(create_args)
            except ValueError:
                tokens = create_args.split('"')
                tokens = [t.strip() for t in tokens if t.strip()]
            if len(tokens) < 1:
                return 'Usage: /spaces create "Name" "Description"'
            name = tokens[0]
            description = tokens[1] if len(tokens) > 1 else ""
            now = utc_now()
            new_space = ContextSpace(
                id=f"space_{_uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name=name,
                description=description,
                space_type="domain",
                status="active",
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(new_space)

            # Initialize compaction state for the new space
            try:
                from kernos.kernel.compaction import (
                    CompactionState, compute_document_budget,
                    MODEL_MAX_TOKENS, COMPACTION_MODEL_USABLE_TOKENS,
                    COMPACTION_INSTRUCTION_TOKENS, DEFAULT_DAILY_HEADROOM,
                )
                headroom = DEFAULT_DAILY_HEADROOM
                doc_budget = compute_document_budget(MODEL_MAX_TOKENS, 4000, 0, headroom)
                comp = CompactionState(
                    space_id=new_space.id,
                    conversation_headroom=headroom,
                    document_budget=doc_budget,
                    message_ceiling=min(
                        doc_budget,
                        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS,
                    ),
                    _context_def_tokens=0,
                    _system_overhead=4000,
                )
                await self.compaction.save_state(tenant_id, new_space.id, comp)
            except Exception as exc:
                logger.warning("Failed to init compaction for manual space: %s", exc)

            logger.info("SPACE_CREATE: id=%s name=%s source=manual", new_space.id, new_space.name)
            return f"Created space **{name}** ({new_space.id}). Description: {description or '(none)'}"

        # Default: list all spaces
        spaces = await self.state.list_context_spaces(tenant_id)
        if not spaces:
            return "No context spaces found."
        lines = ["**Context Spaces**\n"]
        for s in sorted(spaces, key=lambda x: x.last_active_at or "", reverse=True):
            default = " [DEFAULT]" if s.is_default else ""
            lines.append(
                f"- **{s.name}**{default} ({s.id}) — "
                f"type={s.space_type} status={s.status} "
                f"last_active={s.last_active_at or 'never'}"
            )
            if s.description:
                lines.append(f"  {s.description}")
        return "\n".join(lines)

    async def _handle_status(self, ctx: TurnContext) -> str:
        """Write operator state view to diagnostic file, return summary."""
        from kernos.kernel.introspection import build_operator_state_view

        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        from kernos.utils import utc_now
        ts = utc_now()[:19].replace(":", "-")
        status_path = Path(data_dir) / "diagnostics" / f"status_{ts}.txt"
        status_path.parent.mkdir(parents=True, exist_ok=True)

        trigger_store = getattr(self.reasoning, '_trigger_store', None)
        operator_view = await build_operator_state_view(
            ctx.tenant_id, self.state, trigger_store, self.registry,
        )

        with open(status_path, "w") as f:
            f.write(f"=== OPERATOR STATE VIEW ===\n")
            f.write(f"Tenant: {ctx.tenant_id}\n")
            f.write(f"Generated: {utc_now()}\n\n")
            f.write(operator_view)

            # Phase timing averages
            avgs = self.get_phase_timing_averages()
            if avgs:
                f.write("\n\n## Phase Timing (session averages)\n")
                f.write(f"Turns sampled: {len(self._phase_timing_history)}\n")
                for phase in ["provision", "route", "assemble", "reason", "consequence", "persist", "total"]:
                    if phase in avgs:
                        f.write(f"- {phase}: {avgs[phase]}ms\n")

        logger.info("STATUS: state view written to %s", status_path)
        return f"State view written to {status_path}"

    async def _build_departure_context(
        self, ctx: TurnContext, prev_space_id: str,
    ) -> dict | None:
        """Build ephemeral context from departing space for discourse continuity.

        Bounded by both count (up to 6 entries / 3 pairs) and character
        budget (~1200 chars / ~300 tokens). Not persisted to the new space.
        """
        if not prev_space_id or prev_space_id == ctx.active_space_id:
            return None

        # read_recent returns [{role, content, timestamp, channel}, ...]
        recent = await self.conv_logger.read_recent(
            ctx.tenant_id, prev_space_id, token_budget=1200, max_messages=6,
        )
        if not recent:
            return None

        DEPARTURE_CHAR_BUDGET = 1200
        PER_MSG_CAP = 300

        prev_space = await self.state.get_context_space(ctx.tenant_id, prev_space_id)
        prev_name = prev_space.name if prev_space else prev_space_id

        # Walk backward, stop when budget exhausted
        selected: list[dict] = []
        char_total = 0
        for entry in reversed(recent):
            content = entry.get("content", "")[:PER_MSG_CAP]
            if char_total + len(content) > DEPARTURE_CHAR_BUDGET and selected:
                break
            selected.insert(0, entry)
            char_total += len(content)

        if not selected:
            return None

        lines = [f"[Previous context — from space: {prev_name}]"]
        for entry in selected:
            role = entry.get("role", "?")
            content = entry.get("content", "")[:PER_MSG_CAP]
            label = "User" if role == "user" else "Assistant"
            lines.append(f"[{label}]: {content}")
        lines.append(f"[Conversation continues in current space: {ctx.active_space.name if ctx.active_space else ctx.active_space_id}]")

        logger.info("DEPARTURE_CONTEXT: from=%s entries=%d chars=%d",
            prev_space_id, len(selected), char_total)
        return {"role": "user", "content": "\n".join(lines)}

    async def _phase_provision(self, ctx: TurnContext) -> None:
        """Phase 1: Ensure tenant, soul, MCP config, covenants, evaluator ready."""
        tenant_id = ctx.tenant_id
        message = ctx.message
        await self.tenants.get_or_create(tenant_id)
        await self._ensure_tenant_state(tenant_id, message)
        ctx.soul = await self._get_or_init_soul(tenant_id)
        await self._maybe_load_mcp_config(tenant_id)
        await self._maybe_run_covenant_cleanup(tenant_id)
        await self._maybe_start_evaluator(tenant_id)

    async def _phase_route(self, ctx: TurnContext) -> None:
        """Phase 2: Determine context space, handle space switching, file uploads."""
        tenant_id = ctx.tenant_id
        message = ctx.message
        conversation_id = ctx.conversation_id

        recent_full = await self.conversations.get_recent_full(tenant_id, conversation_id, limit=20)
        tenant_profile = await self.state.get_tenant_profile(tenant_id)
        current_focus_id = tenant_profile.last_active_space_id if tenant_profile else ""

        logger.info(
            "ROUTE_INPUT: message=%s recent=%d current_focus=%s",
            (message.content or "")[:80], len(recent_full), current_focus_id or "none",
        )
        ctx.router_result = await self._router.route(tenant_id, message.content, recent_full, current_focus_id)
        ctx.active_space_id = ctx.router_result.focus
        ctx.previous_space_id = current_focus_id
        ctx.space_switched = (
            ctx.active_space_id != ctx.previous_space_id
            and ctx.previous_space_id != ""
            and ctx.active_space_id != ""
        )

        logger.info("USER_MSG: sender=%s full_text=%r", message.sender, message.content)
        _route_space_name = ""
        if ctx.active_space_id:
            _route_space = await self.state.get_context_space(tenant_id, ctx.active_space_id)
            _route_space_name = _route_space.name if _route_space else ""
        logger.info(
            "ROUTE: space=%s (%s) tags=%s confident=%s prev=%s switched=%s router=llm",
            ctx.active_space_id, _route_space_name or "unknown",
            ctx.router_result.tags, ctx.router_result.continuation,
            ctx.previous_space_id, ctx.space_switched,
        )

        if ctx.space_switched:
            import asyncio
            _prev_space = await self.state.get_context_space(tenant_id, ctx.previous_space_id)
            _prev_name = _prev_space.name if _prev_space else "unknown"
            logger.info(
                "SPACE_SWITCH: from=%s (%s) to=%s (%s)",
                ctx.previous_space_id, _prev_name,
                ctx.active_space_id, _route_space_name or "unknown",
            )
            asyncio.create_task(self._run_session_exit(tenant_id, ctx.previous_space_id, conversation_id))
            # Harvest facts from departing space
            try:
                from kernos.kernel.fact_harvest import harvest_facts
                log_text = await self.conv_logger.read_current_log_text(tenant_id, ctx.previous_space_id)
                if isinstance(log_text, tuple):
                    log_text = log_text[0]
                asyncio.create_task(harvest_facts(
                    self.reasoning, self.state, self.events,
                    tenant_id, ctx.previous_space_id, log_text or "",
                    data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
                ))
            except Exception:
                pass

        if tenant_profile and ctx.active_space_id and ctx.active_space_id != ctx.previous_space_id:
            tenant_profile.last_active_space_id = ctx.active_space_id
            await self.state.save_tenant_profile(tenant_id, tenant_profile)

        if ctx.space_switched:
            try:
                await emit_event(self.events, EventType.CONTEXT_SPACE_SWITCHED, tenant_id, "router",
                    payload={"from_space": ctx.previous_space_id, "to_space": ctx.active_space_id,
                             "router_tags": ctx.router_result.tags, "continuation": ctx.router_result.continuation})
            except Exception as exc:
                logger.warning("Failed to emit context.space.switched: %s", exc)

        # Gate 1: topic hint tracking
        known_space_ids = {s.id for s in await self.state.list_context_spaces(tenant_id)}
        for tag in ctx.router_result.tags:
            if tag and tag not in known_space_ids:
                try:
                    await self.state.increment_topic_hint(tenant_id, tag)
                    count = await self.state.get_topic_hint_count(tenant_id, tag)
                    if count >= SPACE_CREATION_THRESHOLD:
                        import asyncio
                        asyncio.create_task(self._trigger_gate2(tenant_id, tag, conversation_id))
                except Exception as exc:
                    logger.warning("Gate 1 tracking failed for hint '%s': %s", tag, exc)

        ctx.active_space = (
            await self.state.get_context_space(tenant_id, ctx.active_space_id)
            if ctx.active_space_id else None
        )
        if ctx.active_space and ctx.active_space_id:
            await self.state.update_context_space(tenant_id, ctx.active_space_id,
                {"last_active_at": utc_now(), "status": "active"})

        if message.context and ctx.active_space_id:
            for att in message.context.get("attachments", []):
                note = await self._handle_file_upload(tenant_id, ctx.active_space_id,
                    att.get("filename", "upload.txt"), att.get("content", ""))
                ctx.upload_notifications.append(note)

    async def _phase_assemble(self, ctx: TurnContext) -> None:
        """Phase 3: Build Cognitive UI blocks — system prompt, tools, messages."""
        tenant_id = ctx.tenant_id
        message = ctx.message
        soul = ctx.soul
        active_space = ctx.active_space
        active_space_id = ctx.active_space_id

        # Space context (compaction, cross-domain, system events, receipts)
        space_messages, ctx.results_prefix, ctx.memory_prefix = await self._assemble_space_context(
            tenant_id, ctx.conversation_id, active_space_id, active_space
        )

        # Emit message.received
        try:
            await emit_event(self.events, EventType.MESSAGE_RECEIVED, tenant_id, "handler",
                payload={"content": message.content, "sender": message.sender,
                         "sender_auth_level": message.sender_auth_level.value,
                         "platform": message.platform, "conversation_id": ctx.conversation_id})
        except Exception as exc:
            logger.warning("Failed to emit message.received: %s", exc)

        # Store user message
        user_content = message.content
        if not user_content or not user_content.strip():
            if ctx.upload_notifications:
                filenames = [att.get("filename", "file") for att in (message.context or {}).get("attachments", [])]
                user_content = "User uploaded: " + ", ".join(filenames) if filenames else "User uploaded a file."
            else:
                user_content = "(empty message)"
            logger.info("EMPTY_MSG_GUARD: injected content=%r for empty user message", user_content)

        # Skip persisting diagnostic commands — they shouldn't appear in conversation history
        _is_diagnostic = user_content.strip().lower().split()[0] in ("/dump", "/status", "/help", "/spaces") if user_content.strip() else False
        if not _is_diagnostic:
            user_entry = {
                "role": "user", "content": user_content,
                "timestamp": message.timestamp.isoformat(), "platform": message.platform,
                "tenant_id": tenant_id, "conversation_id": ctx.conversation_id,
                "space_tags": ctx.router_result.tags,
            }
            await self.conversations.append(tenant_id, ctx.conversation_id, user_entry)
            await self.conv_logger.append(tenant_id=tenant_id, space_id=active_space_id,
                speaker="user", channel=message.platform, content=user_content,
                timestamp=message.timestamp.isoformat())

        # --- Concurrent cohort agents -----------------------------------------
        # Preference parsing and knowledge shaping are independent LLM pipelines.
        # Running them concurrently via asyncio.gather cuts ~2-3s from assembly.

        async def _run_pref_parsing() -> str | None:
            """Preference detection — in-turn compile and commit (Phase 6A-4)."""
            if not (self.preference_parsing_enabled and not _is_diagnostic and user_content.strip()):
                return None
            try:
                from kernos.kernel.preference_parser import parse_preferences_in_message
                trigger_store = getattr(self.reasoning, '_trigger_store', None)
                return await parse_preferences_in_message(
                    user_content, tenant_id, active_space_id,
                    self.state, self.reasoning, trigger_store,
                )
            except Exception as exc:
                logger.warning("PREF_DETECT: pipeline error: %s", exc)
                return None

        async def _run_knowledge_pipeline() -> list:
            """Three-tier selective knowledge injection (SPEC-SELECTIVE-KNOWLEDGE-INJECTION)."""
            all_ke = await self.state.query_knowledge(tenant_id, subject="user", active_only=True, limit=200)

            # Tier 1: Always inject — identity facts (name, age, timezone, location)
            always_inject = [e for e in all_ke if e.lifecycle_archetype == "identity"]

            # Tier 2: Never inject — ephemeral, expired, stale contextual
            _never_archetypes = {"ephemeral"}
            candidates = [
                e for e in all_ke
                if e not in always_inject
                and e.lifecycle_archetype not in _never_archetypes
                and not getattr(e, "expired_at", "")
                and not (e.lifecycle_archetype == "contextual"
                         and _is_stale_knowledge(e, days=14))
            ]

            # Tier 3: LLM shaping — select relevant entries for this turn
            shaped = []
            if candidates:
                relevant_ids = await self._shape_knowledge(candidates, message, ctx)
                shaped = [e for e in candidates if e.id in relevant_ids]

            return always_inject + shaped

        # Fire both LLM pipelines + covenant query concurrently
        space_scope = [active_space_id, None] if active_space_id else None
        pref_note, user_knowledge_entries, contract_rules = await asyncio.gather(
            _run_pref_parsing(),
            _run_knowledge_pipeline(),
            self.state.query_covenant_rules(
                tenant_id, context_space_scope=space_scope, active_only=True),
        )

        if pref_note:
            ctx.pref_detected = True
            if ctx.results_prefix:
                ctx.results_prefix += "\n\n" + pref_note
            else:
                ctx.results_prefix = pref_note

        # --- Tool surfacing (CPU-bound, no LLM) --------------------------------
        from kernos.kernel.reasoning import REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL, MANAGE_CAPABILITIES_TOOL
        from kernos.kernel.awareness import DISMISS_WHISPER_TOOL

        # Tier 1: Always-surface (minimal universal kernel tools)
        tools: list[dict] = [REQUEST_TOOL, READ_DOC_TOOL, DISMISS_WHISPER_TOOL,
                              MANAGE_CAPABILITIES_TOOL, REMEMBER_DETAILS_TOOL]
        if self._retrieval:
            from kernos.kernel.retrieval import REMEMBER_TOOL
            tools.append(REMEMBER_TOOL)

        # Tier 2: Category-surfaced based on turn context
        recent_topic = self._get_recent_topic_hint(ctx)
        categories = _select_tool_categories(message.content or "", recent_topic)

        # Collect category-matched tool names
        surfaced_names: set[str] = set()
        for cat in categories:
            surfaced_names.update(CATEGORY_TOOLS.get(cat, set()))

        # Session continuity: already-loaded tools always surface
        loaded_names = self.reasoning.get_loaded_tools(active_space_id)
        surfaced_names.update(loaded_names)

        # Space-activated capabilities: tools from capabilities explicitly activated
        # for this space (via request_tool) persist across turns without keyword matching
        if active_space and active_space.active_tools:
            for cap_name in active_space.active_tools:
                cap = self.registry.get(cap_name)
                if cap and cap.tools:
                    surfaced_names.update(cap.tools)

        # Map surfaced names to actual tool schemas
        # Kernel tools matched by category
        _kernel_tool_map: dict[str, dict] = {}
        from kernos.kernel.files import FILE_TOOLS
        from kernos.kernel.reasoning import READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL
        from kernos.kernel.covenant_manager import MANAGE_COVENANTS_TOOL
        from kernos.kernel.channels import MANAGE_CHANNELS_TOOL, SEND_TO_CHANNEL_TOOL
        from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
        for t in FILE_TOOLS + [READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL,
                                MANAGE_COVENANTS_TOOL, MANAGE_CHANNELS_TOOL,
                                SEND_TO_CHANNEL_TOOL, MANAGE_SCHEDULE_TOOL]:
            _kernel_tool_map[t["name"]] = t

        for name in surfaced_names:
            if name in _kernel_tool_map:
                # Add kernel tool if not already in Tier 1
                kt = _kernel_tool_map[name]
                if kt not in tools:
                    tools.append(kt)

        # MCP tools: preloaded (full schema) + loaded (full) + stubs for category matches
        preloaded = self.registry.get_preloaded_tools(space=active_space)
        for t in preloaded:
            if t["name"] in surfaced_names and t not in tools:
                tools.append(t)
        for tn in loaded_names:
            schema = self.registry.get_tool_schema(tn)
            if schema and schema not in tools:
                tools.append(schema)
        # Stubs only for category-matched MCP tools not already loaded
        all_stubs = self.registry.get_lazy_tool_stubs(space=active_space, loaded_names=loaded_names)
        for stub in all_stubs:
            if stub["name"] in surfaced_names and stub not in tools:
                tools.append(stub)

        _tier1_count = 6 if self._retrieval else 5  # Tier 1 kernel tools always present
        _total = _tier1_count + len(_kernel_tool_map) + len(preloaded) + len(all_stubs) + len(loaded_names)

        # Stable sort: Tier 1 kernel tools first (fixed prefix), then rest alphabetical.
        # Same tool set always produces identical ordering — maximizes cache hits (IQ-3).
        _tier1_names = {t.get("name") for t in tools[:_tier1_count]}
        _tier1 = [t for t in tools if t.get("name") in _tier1_names]
        _rest = [t for t in tools if t.get("name") not in _tier1_names]
        _tier1.sort(key=lambda t: t.get("name", ""))
        _rest.sort(key=lambda t: t.get("name", ""))
        tools = _tier1 + _rest

        logger.info("TOOL_SURFACING: categories=%s surfaced=%d total_available=%d",
            categories, len(tools), _total)
        ctx.tools = tools

        # Build system prompt blocks (Cognitive UI grammar)
        capability_prompt = self.registry.build_tool_directory(space=active_space)

        # Inject merge note so agent knows multiple messages need addressing
        if ctx.merged_count > 1:
            merge_note = (
                f"IMPORTANT: This turn contains {ctx.merged_count} user messages "
                f"(merged from rapid input). You MUST address ALL of them in your "
                f"response. Do not skip any. Read through all the user messages in "
                f"the conversation before responding."
            )
            if ctx.results_prefix:
                ctx.results_prefix += "\n\n" + merge_note
            else:
                ctx.results_prefix = merge_note

        rules = _build_rules_block(PRIMARY_TEMPLATE, contract_rules, soul)
        now_block = _build_now_block(message, soul, active_space)
        state_block = _build_state_block(soul, PRIMARY_TEMPLATE, user_knowledge_entries)
        results = _build_results_block(ctx.results_prefix)
        actions = _build_actions_block(capability_prompt, message, self._channel_registry)
        memory = _build_memory_block(ctx.memory_prefix)

        # Cache boundary: static prefix (RULES + ACTIONS) is stable across turns,
        # dynamic suffix (NOW + STATE + RESULTS + MEMORY) changes every turn.
        # Reorder so static comes first for provider-level prompt caching.
        ctx.system_prompt_static = _compose_blocks(rules, actions)
        ctx.system_prompt_dynamic = _compose_blocks(now_block, state_block, results, memory)
        ctx.system_prompt = _compose_blocks(ctx.system_prompt_static, ctx.system_prompt_dynamic)

        # Developer mode: inject pending errors
        tenant_profile = await self.state.get_tenant_profile(tenant_id)
        if tenant_profile and getattr(tenant_profile, 'developer_mode', False):
            error_block = self._error_buffer.drain(tenant_id)
            if error_block:
                ctx.system_prompt += "\n\n" + error_block

        # Pending trigger deliveries
        try:
            pending_triggers = await self._trigger_store.list_all(tenant_id)
            for trig in pending_triggers:
                if trig.pending_delivery:
                    ctx.upload_notifications.append(
                        f"[Scheduled action result — {trig.action_description}]: {trig.pending_delivery}")
                    trig.pending_delivery = ""
                    await self._trigger_store.save(trig)
        except Exception:
            pass

        # Build messages array (CONVERSATION block — carried by messages, not system prompt)
        final_user_content = message.content
        # Prepend orphaned user messages from rapid-fire input
        orphans = getattr(self, '_orphaned_user_content', None)
        if orphans:
            prefix = "\n".join(f"(Earlier message: {o})" for o in orphans)
            final_user_content = prefix + "\n\n" + (message.content or "")
            self._orphaned_user_content = None
        if ctx.upload_notifications:
            final_user_content = "\n".join(ctx.upload_notifications) + (
                "\n\n" + final_user_content if final_user_content else "")
        # Departure context: ephemeral bridge from departing space on switch
        departure_msg = None
        if ctx.space_switched and ctx.previous_space_id:
            departure_msg = await self._build_departure_context(ctx, ctx.previous_space_id)

        if departure_msg:
            ctx.messages = [departure_msg] + space_messages + [{"role": "user", "content": final_user_content}]
        else:
            ctx.messages = space_messages + [{"role": "user", "content": final_user_content}]

    async def _phase_reason(self, ctx: TurnContext) -> None:
        """Phase 4: Build ReasoningRequest, execute via task engine."""
        ctx.task = Task(
            id=generate_task_id(), type=TaskType.REACTIVE_SIMPLE,
            tenant_id=ctx.tenant_id, conversation_id=ctx.conversation_id,
            source="user_message", input_text=ctx.message.content, created_at=utc_now(),
        )
        request = ReasoningRequest(
            tenant_id=ctx.tenant_id, conversation_id=ctx.conversation_id,
            system_prompt=ctx.system_prompt, messages=ctx.messages, tools=ctx.tools,
            system_prompt_static=ctx.system_prompt_static,
            system_prompt_dynamic=ctx.system_prompt_dynamic,
            model=self.reasoning.main_model,
            trigger="user_message", active_space_id=ctx.active_space_id,
            input_text=ctx.message.content, active_space=ctx.active_space,
            user_timezone=ctx.soul.timezone,
        )
        ctx.task = await self.engine.execute(ctx.task, request)
        ctx.response_text = ctx.task.result_text

    async def _phase_consequence(self, ctx: TurnContext) -> None:
        """Phase 5: Confirmation replay, tool config, projectors, soul update."""
        tenant_id = ctx.tenant_id
        request = ReasoningRequest(
            tenant_id=tenant_id, conversation_id=ctx.conversation_id,
            system_prompt=ctx.system_prompt, messages=ctx.messages, tools=ctx.tools,
            system_prompt_static=ctx.system_prompt_static,
            system_prompt_dynamic=ctx.system_prompt_dynamic,
            model="", trigger="", active_space_id=ctx.active_space_id,
            input_text=ctx.message.content, active_space=ctx.active_space,
        )

        # Confirmation replay
        pending = self.reasoning.get_pending_actions(tenant_id)
        conflict_this_turn = self.reasoning.get_conflict_raised()
        if pending and conflict_this_turn:
            confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
            ctx.response_text = confirm_pattern.sub("", ctx.response_text).strip()
            logger.info("CONFIRM_BLOCKED: tenant=%s reason=same_turn_as_conflict", tenant_id)
        elif pending:
            confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
            matches = confirm_pattern.findall(ctx.response_text)
            if matches:
                actions_to_execute: list[int] = []
                for match in matches:
                    if match.upper() == "ALL":
                        actions_to_execute = list(range(len(pending)))
                        break
                    else:
                        idx = int(match)
                        if 0 <= idx < len(pending) and idx not in actions_to_execute:
                            actions_to_execute.append(idx)
                execution_results: list[str] = []
                for idx in actions_to_execute:
                    action = pending[idx]
                    if datetime.now(timezone.utc) < action.expires_at:
                        try:
                            result = await self.reasoning.execute_tool(action.tool_name, action.tool_input, request)
                            execution_results.append(f"✓ {action.proposed_action}: {result}")
                            logger.info("CONFIRM_EXECUTE: tool=%s idx=%d", action.tool_name, idx)
                        except Exception as exc:
                            execution_results.append(f"Failed: {action.proposed_action} ({exc})")
                            logger.warning("CONFIRM_EXECUTE_FAILED: tool=%s idx=%d error=%s", action.tool_name, idx, exc)
                    else:
                        execution_results.append(f"Expired: {action.proposed_action}")
                        logger.warning("CONFIRM_EXPIRED: tool=%s idx=%d", action.tool_name, idx)
                self.reasoning.clear_pending_actions(tenant_id)
                ctx.response_text = confirm_pattern.sub("", ctx.response_text).strip()
                if execution_results:
                    ctx.response_text += "\n\n" + "\n".join(execution_results)
            else:
                all_expired = all(datetime.now(timezone.utc) >= a.expires_at for a in pending)
                if all_expired:
                    self.reasoning.clear_pending_actions(tenant_id)
                    logger.info("PENDING_CLEARED: tenant=%s reason=all_expired", tenant_id)

        # Tool config persistence
        if self.reasoning.get_tools_changed():
            self.reasoning.reset_tools_changed()
            try:
                await self._persist_mcp_config(tenant_id)
                system_space = await self._get_system_space(tenant_id)
                if system_space:
                    await self._write_capabilities_overview(tenant_id, system_space.id)
            except Exception as exc:
                logger.warning("Failed to persist tools config: %s", exc)

        # Projectors
        history = await self.conversations.get_recent(tenant_id, ctx.conversation_id, limit=20)
        await run_projectors(
            user_message=ctx.message.content, recent_turns=history[-4:],
            soul=ctx.soul, state=self.state, events=self.events,
            reasoning_service=self.reasoning, tenant_id=tenant_id,
            active_space_id=ctx.active_space_id, active_space=ctx.active_space,
        )

        ctx.response_text = _maybe_append_name_ask(ctx.response_text, ctx.soul)
        await self._post_response_soul_update(ctx.soul)

    async def _phase_persist(self, ctx: TurnContext) -> None:
        """Phase 6: Store messages, write to conv log, compaction, events."""
        tenant_id = ctx.tenant_id
        message = ctx.message

        assistant_entry = {
            "role": "assistant", "content": ctx.response_text,
            "timestamp": utc_now(), "platform": message.platform,
            "tenant_id": tenant_id, "conversation_id": ctx.conversation_id,
            "space_tags": ctx.router_result.tags,
        }
        await self.conversations.append(tenant_id, ctx.conversation_id, assistant_entry)
        await self.conv_logger.append(tenant_id=tenant_id, space_id=ctx.active_space_id,
            speaker="assistant", channel=message.platform, content=ctx.response_text)

        # Compaction (with concurrency guard + backoff)
        if ctx.active_space_id in self._compacting:
            logger.info("COMPACTION: already in progress for space=%s, skipping", ctx.active_space_id)
        else:
            try:
                comp_state = await self.compaction.load_state(tenant_id, ctx.active_space_id)
                if comp_state:
                    _skip = False
                    if comp_state.consecutive_failures > 0 and comp_state.last_compaction_failure_at:
                        _backoff_s = min(60 * (2 ** (comp_state.consecutive_failures - 1)), 900)
                        try:
                            _last_fail = datetime.fromisoformat(comp_state.last_compaction_failure_at)
                            if (datetime.now(timezone.utc) - _last_fail).total_seconds() < _backoff_s:
                                _skip = True
                        except (ValueError, TypeError):
                            pass
                    if not _skip:
                        log_info = await self.conv_logger.get_current_log_info(tenant_id, ctx.active_space_id)
                        new_tokens = log_info["tokens_est"] - log_info.get("seeded_tokens_est", 0)
                        _real_ctx = self.reasoning.get_last_real_input_tokens(tenant_id)
                        logger.info(
                            "COMPACTION_INPUT: space=%s tokens_est=%d threshold=%d real_ctx=%d",
                            ctx.active_space_id, new_tokens, comp_state.compaction_threshold, _real_ctx,
                        )
                        if new_tokens >= comp_state.compaction_threshold:
                            log_text, log_num = await self.conv_logger.read_current_log_text(tenant_id, ctx.active_space_id)
                            if log_text.strip() and ctx.active_space:
                                self._compacting.add(ctx.active_space_id)
                                # UX signal: notify user on Discord (not SMS)
                                if message.platform == "discord":
                                    try:
                                        await self.send_outbound(
                                            tenant_id, ctx.member_id, "discord",
                                            "(Compacting...)",
                                        )
                                    except Exception:
                                        pass
                                try:
                                    # Harvest durable facts before compaction
                                    try:
                                        from kernos.kernel.fact_harvest import harvest_facts
                                        await harvest_facts(
                                            self.reasoning, self.state, self.events,
                                            tenant_id, ctx.active_space_id, log_text,
                                            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
                                        )
                                    except Exception as _hx:
                                        logger.warning("FACT_HARVEST: pre-compaction failed: %s", _hx)

                                    comp_state = await self.compaction.compact_from_log(
                                        tenant_id, ctx.active_space_id, ctx.active_space, log_text, log_num, comp_state)
                                    old_num, new_num = await self.conv_logger.roll_log(tenant_id, ctx.active_space_id)
                                    await self.conv_logger.seed_from_previous(tenant_id, ctx.active_space_id, old_num, tail_entries=10)
                                    self.reasoning.clear_loaded_tools(ctx.active_space_id)
                                    comp_state.consecutive_failures = 0
                                    comp_state.last_compaction_failure_at = ""
                                    logger.info("COMPACTION_COMPLETE: space=%s source=log_%03d new_log=log_%03d",
                                        ctx.active_space_id, old_num, new_num)
                                    # Domain assessment + child briefings — async, non-blocking
                                    try:
                                        import asyncio as _aio
                                        _aio.create_task(self._assess_domain_creation(
                                            tenant_id, ctx.active_space_id, ctx.active_space, comp_state))
                                        _aio.create_task(self._produce_child_briefings(
                                            tenant_id, ctx.active_space_id, ctx.active_space))
                                    except Exception as _dax:
                                        logger.warning("DOMAIN_ASSESS/BRIEFING: launch failed: %s", _dax)
                                finally:
                                    self._compacting.discard(ctx.active_space_id)
                        else:
                            await self.compaction.save_state(tenant_id, ctx.active_space_id, comp_state)
            except Exception as exc:
                logger.warning("COMPACTION: failed for space=%s: %s", ctx.active_space_id, exc)
                try:
                    comp_state = await self.compaction.load_state(tenant_id, ctx.active_space_id)
                    if comp_state:
                        comp_state.consecutive_failures += 1
                        comp_state.last_compaction_failure_at = utc_now()
                        await self.compaction.save_state(tenant_id, ctx.active_space_id, comp_state)
                except Exception:
                    pass
                self._compacting.discard(ctx.active_space_id)

        # Emit message.sent
        try:
            await emit_event(self.events, EventType.MESSAGE_SENT, tenant_id, "handler",
                payload={"content": ctx.response_text, "conversation_id": ctx.conversation_id, "platform": message.platform})
        except Exception as exc:
            logger.warning("Failed to emit message.sent: %s", exc)

        await self._update_conversation_summary(tenant_id, ctx.conversation_id, message.platform)

    def _record_phase_timings(self, timings: dict[str, int], total_ms: int) -> None:
        """Record phase timings for session averages. Keep last 50 turns."""
        entry = dict(timings)
        entry["total"] = total_ms
        self._phase_timing_history.append(entry)
        if len(self._phase_timing_history) > 50:
            self._phase_timing_history = self._phase_timing_history[-50:]

    def get_phase_timing_averages(self) -> dict[str, int]:
        """Return average phase timings across the session."""
        if not self._phase_timing_history:
            return {}
        phases = ["provision", "route", "assemble", "reason", "consequence", "persist", "total"]
        avgs: dict[str, int] = {}
        for phase in phases:
            values = [t.get(phase, 0) for t in self._phase_timing_history if phase in t]
            if values:
                avgs[phase] = sum(values) // len(values)
        return avgs

    async def _run_friction_observer(
        self, ctx: TurnContext, provider_errors: list[str] | None = None,
    ) -> None:
        """Run friction detection post-turn. Non-blocking — failures are logged and swallowed."""
        try:
            surfaced_names = {t.get("name", "") for t in ctx.tools if t.get("name")}
            await self._friction.observe(
                tenant_id=ctx.tenant_id,
                user_message=ctx.message.content or "",
                response_text=ctx.response_text,
                tool_trace=ctx.tool_calls_trace,
                surfaced_tool_names=surfaced_names,
                active_space_id=ctx.active_space_id,
                merged_count=ctx.merged_count,
                is_reactive=True,
                pref_detected=ctx.pref_detected,
                provider_errors=provider_errors,
                has_now_block_time=True,
            )
        except Exception as exc:
            logger.debug("FRICTION: observer failed: %s", exc)

    # --- Selective knowledge injection helpers ---

    async def _shape_knowledge(
        self, candidates: list, message: NormalizedMessage, ctx: TurnContext,
    ) -> set[str]:
        """Use cheap LLM to select relevant knowledge entries for this turn.

        Returns set of entry IDs to inject. On failure, returns empty set
        (Tier 1 only fallback — NOT full Tier 3 dump).
        """
        try:
            candidate_lines = "\n".join(
                f"- [{e.id}] \"{e.content}\" ({e.lifecycle_archetype})"
                for e in candidates
            )
            recent_topic = self._get_recent_topic_hint(ctx)

            logger.info(
                "SHAPE_INPUT: candidates=%d message=%s",
                len(candidates), (message.content or "")[:80],
            )
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    "Select which user knowledge entries are relevant to "
                    "this conversation turn. Return ONLY the IDs of relevant "
                    "entries as a comma-separated list, or NONE if nothing "
                    "is relevant.\nExample: know_abc, know_def"
                ),
                user_content=(
                    f"User's message: \"{message.content[:200]}\"\n"
                    f"Recent topic: {recent_topic}\n\n"
                    f"Candidates:\n{candidate_lines}"
                ),
                max_tokens=128,
                prefer_cheap=True,
            )

            if not result or "NONE" in result.upper():
                return set()

            ids: set[str] = set()
            for token in result.replace(",", " ").split():
                token = token.strip()
                if token.startswith("know_"):
                    ids.add(token)
            logger.info("KNOWLEDGE_SHAPED: selected=%d/%d ids=%s",
                        len(ids), len(candidates), ",".join(sorted(ids)[:5]))
            return ids
        except Exception as exc:
            logger.warning("KNOWLEDGE_SHAPING_FAILED: %s — falling back to Tier 1 only", exc)
            return set()  # fail-safe: Tier 1 only, NOT full dump

    def _get_recent_topic_hint(self, ctx: TurnContext) -> str:
        """Extract a brief topic hint from recent conversation."""
        if not ctx.messages:
            return "new conversation"
        recent = ctx.messages[-3:]
        texts = [m.get("content", "")[:100] for m in recent
                 if isinstance(m.get("content"), str)]
        return " | ".join(texts)[-200:] if texts else "general"

    async def _handle_reasoning_error(self, ctx: TurnContext, exc: Exception, user_msg: str) -> str:
        """Handle reasoning errors with event emission and user-facing message."""
        logger.error("Reasoning error for sender=%s: %s", ctx.message.sender, exc, exc_info=True)
        try:
            stage = "api_call" if not isinstance(exc, Exception) or isinstance(exc, (
                ReasoningTimeoutError, ReasoningConnectionError, ReasoningRateLimitError, ReasoningProviderError
            )) else "general"
            await emit_event(self.events, EventType.HANDLER_ERROR, ctx.tenant_id, "handler",
                payload={"error_type": type(exc).__name__, "error_message": str(exc),
                         "conversation_id": ctx.conversation_id, "stage": stage})
        except Exception:
            pass
        return f"Something went wrong on my end — {user_msg}."
