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


def _format_contracts(rules: list[CovenantRule], space_names: dict[str, str] | None = None) -> str:
    """Format behavioral contract rules with source attribution for the system prompt."""
    if not rules:
        return ""
    _names = space_names or {}
    lines = ["BEHAVIORAL CONTRACTS — follow these strictly:"]
    for rule in rules:
        label = rule.rule_type.replace("_", " ").upper()
        scope_tag = ""
        if rule.context_space:
            scope_tag = f" [{_names.get(rule.context_space, rule.context_space)}]"
        else:
            scope_tag = " [global]"
        lines.append(f"{label}: {rule.description}{scope_tag}")
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
def _is_similar_topic(new_name: str, existing_names: list[str]) -> bool:
    """Check if a proposed domain name is similar to existing names (drift detection).

    Returns True if >50% of words overlap — likely a rename, not a new domain.
    """
    new_words = set(new_name.lower().split())
    if not new_words:
        return False
    for name in existing_names:
        existing_words = set(name.lower().split())
        overlap = new_words & existing_words
        if len(overlap) > 0 and len(overlap) >= len(new_words) * 0.5:
            return True
    return False




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
    space_names: dict[str, str] | None = None,
) -> str:
    """## RULES — operating principles + behavioral contracts + bootstrap."""
    parts = [template.operating_principles]
    contracts_text = _format_contracts(contract_rules, space_names)
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
        _SOURCE_TAGS = {
            "identity": "stated", "habitual": "observed",
            "structural": "established", "episodic": "remembered",
            "contextual": "recent",
        }
        seen_content: set[str] = set()
        for entry in user_knowledge_entries:
            normalized = entry.content.strip().lower()
            if normalized in seen_content:
                continue
            # Filter out entries that confuse agent identity with user identity
            if agent_name.lower() in normalized and "user" in normalized and "name" in normalized:
                continue
            seen_content.add(normalized)
            tag = _SOURCE_TAGS.get(getattr(entry, "lifecycle_archetype", ""), "known")
            user_parts.append(f"{entry.content} [{tag}]")
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


def _build_procedures_block(procedures_prefix: str | None) -> str:
    """## PROCEDURES — domain-specific workflows from _procedures.md."""
    if not procedures_prefix:
        return ""
    return "## PROCEDURES\n" + procedures_prefix


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
        self._turn_counter: int = 0  # monotonic turn counter for tool LRU tracking
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
        self._files = FileService(os.getenv("KERNOS_DATA_DIR", "./data"), state=self.state)
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

        # Tool catalog — universal registry for three-tier surfacing
        from kernos.kernel.tool_catalog import ToolCatalog
        self._tool_catalog = ToolCatalog()
        self._register_kernel_tools_in_catalog()

        # Workspace manager — artifact lifecycle, tool registration, lazy manifest loading
        from kernos.kernel.workspace import WorkspaceManager
        self._workspace = WorkspaceManager(
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            catalog=self._tool_catalog,
        )
        reasoning.set_workspace(self._workspace)

    def _register_kernel_tools_in_catalog(self) -> None:
        """Register kernel tools in the universal catalog at boot."""
        _kernel_descs = {
            "request_tool": "Request access to a specific tool or capability",
            "read_doc": "Read system documentation pages",
            "dismiss_whisper": "Dismiss a proactive awareness whisper",
            "manage_capabilities": "List, enable, or disable capability connections",
            "remember_details": "Search memory for detailed information",
            "remember": "Search memory and knowledge base",
            "write_file": "Create or update a text file in the current space",
            "read_file": "Read a file from the current or parent space",
            "list_files": "List all files including inherited from parents",
            "delete_file": "Soft-delete a file",
            "read_source": "Read system source code for debugging",
            "read_soul": "Read the agent's personality and identity configuration",
            "update_soul": "Update agent personality or identity",
            "manage_covenants": "List, add, update, or remove standing rules",
            "manage_channels": "List or configure messaging channels",
            "send_to_channel": "Send a message to an outbound channel (SMS, etc.)",
            "manage_schedule": "View and manage scheduled triggers and automations",
            "inspect_state": "View active preferences, triggers, and rules",
            "execute_code": "Execute Python code in a sandboxed environment for building tools and running computations",
            "manage_workspace": "Manage workspace artifacts — list, add, update, or archive built tools and scripts",
            "register_tool": "Register a workspace-built tool in the universal catalog from a .tool.json descriptor",
        }
        for name, desc in _kernel_descs.items():
            self._tool_catalog.register(name, desc, "kernel")

    def register_mcp_tools_in_catalog(self) -> None:
        """Register MCP tools in the catalog. Called after MCP connect_all."""
        if not self.mcp:
            return
        for tool in self.mcp.get_tools():
            name = tool.get("name", "")
            desc = tool.get("description", "")
            # Truncate to one line
            if desc:
                desc = desc.split(".")[0].strip()[:100]
            else:
                desc = name.replace("-", " ").replace("_", " ")
            self._tool_catalog.register(name, desc, f"mcp")

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
    ) -> tuple[list[dict], str | None, str | None, str | None]:
        """Assemble the agent's conversation context for the active space.

        Returns (recent_messages, results_prefix, memory_prefix, procedures_prefix) where:
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

        # 2b. Cross-domain notices → RESULTS (one-time delivery)
        try:
            notices = await self.state.drain_space_notices(tenant_id, active_space_id)
            if notices:
                notice_lines = [n["text"] for n in notices if n.get("text")]
                if notice_lines:
                    results_parts.append(
                        "CROSS-DOMAIN UPDATES:\n" + "\n".join(notice_lines)
                    )
                    logger.info("CROSS_DOMAIN_DELIVER: space=%s notices=%d", active_space_id, len(notice_lines))
        except Exception as exc:
            logger.warning("CROSS_DOMAIN_DELIVER: failed: %s", exc)

        # 2c. System events → RESULTS
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

        # 5. Procedure files from scope chain → PROCEDURES section
        procedures_prefix = None
        if active_space and active_space_id:
            try:
                proc_parts: list[str] = []
                # Build scope chain for procedure inheritance
                _proc_chain = [active_space_id]
                _cur_space = active_space
                while _cur_space and _cur_space.parent_id:
                    _proc_chain.append(_cur_space.parent_id)
                    _cur_space = await self.state.get_context_space(tenant_id, _cur_space.parent_id)
                for sid in _proc_chain:
                    content = await self._files.read_file(tenant_id, sid, "_procedures.md")
                    if content and not content.startswith("Error:"):
                        if sid == active_space_id:
                            proc_parts.append(content)
                        else:
                            _pspace = await self.state.get_context_space(tenant_id, sid)
                            _pname = _pspace.name if _pspace else sid
                            proc_parts.append(f"[From {_pname}]\n{content}")
                if proc_parts:
                    procedures_prefix = "\n\n".join(proc_parts)
            except Exception as exc:
                logger.warning("PROCEDURES_LOAD: failed for space=%s: %s", active_space_id, exc)

        # 6. Recent messages — read from space log (P2), fallback to legacy store
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

        return recent_messages, results_prefix, memory_prefix, procedures_prefix

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


    # --- Domain assessment (CS-2) ---

    DOMAIN_ASSESSMENT_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "create_domain": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "posture": {"type": "string",
                        "description": "Brief working style for this domain. "
                        "How should the agent approach work here? One sentence. "
                        "Examples: 'Creative and improvisational', "
                        "'Precise and action-oriented', 'Warm and supportive'."},
            "reasoning": {"type": "string"},
            "rename": {"type": "boolean"},
            "new_name": {"type": "string"},
            "rename_evidence": {"type": "string"},
            "migrate_covenants": {
                "type": "array", "items": {"type": "string"},
                "description": "IDs of parent covenants that belong in the new domain",
            },
            "migrate_files": {
                "type": "array", "items": {"type": "string"},
                "description": "Filenames from parent that belong in the new domain",
            },
            "migrate_procedure_sections": {
                "type": "array", "items": {"type": "string"},
                "description": "Section titles from parent _procedures.md that belong in the new domain",
            },
        },
        "required": ["create_domain", "confidence", "name", "description", "posture", "reasoning", "rename", "new_name", "rename_evidence", "migrate_covenants", "migrate_files", "migrate_procedure_sections"],
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

        # Build parent content inventory for migration assessment
        _inv_parts: list[str] = []
        try:
            _parent_rules = await self.state.query_covenant_rules(
                tenant_id, context_space_scope=[space_id], active_only=True)
            if _parent_rules:
                _inv_parts.append("Covenants:\n" + "\n".join(
                    f"  [{r.id}] {r.rule_type}: {r.description}" for r in _parent_rules))
            _parent_manifest = await self._files.load_manifest(tenant_id, space_id)
            if _parent_manifest:
                _inv_parts.append("Files:\n" + "\n".join(
                    f"  {fname}: {desc}" for fname, desc in _parent_manifest.items() if not fname.startswith(".")))
            _parent_procs = await self._files.read_file(tenant_id, space_id, "_procedures.md")
            if _parent_procs and not _parent_procs.startswith("Error:"):
                _sections = [line.strip() for line in _parent_procs.split("\n") if line.startswith("## ")]
                if _sections:
                    _inv_parts.append("Procedure sections:\n" + "\n".join(f"  {s}" for s in _sections))
        except Exception:
            pass
        _parent_inventory = "\n".join(_inv_parts) if _inv_parts else "(no content to migrate)"

        child_type = "domain" if space.depth == 0 else "subdomain"

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are assessing whether a conversation belongs in its own "
                    f"dedicated context {child_type}, or should remain in the current space.\n\n"
                    "Domains can come from ANY area of someone's life — business, legal, "
                    "health, family, finance, creative work, property, education, hobbies, "
                    "relationships, or anything else with recurring depth.\n\n"
                    "Only create on HIGH confidence. A domain should:\n"
                    "- Have clear internal coherence (not a grab-bag)\n"
                    "- Likely recur in future conversations\n"
                    "- Benefit from isolated context (for BOTH domain AND parent)\n"
                    "- Have a stable, clear label\n\n"
                    "A single conversation about a topic is NOT enough. "
                    "The topic must have depth and likely recurrence.\n"
                    '"Kitchen Renovation" is a domain. "Tax Prep 2026" is a domain. '
                    '"Dog Training" is a domain. "Random questions" is not.\n\n'
                    "RENAME CHECK: Has the user indicated a NAME CHANGE for this space? "
                    'Look for explicit statements like "let\'s call it X" or "we\'re renaming to X." '
                    "If yes, set rename=true, new_name to the new name, and rename_evidence.\n\n"
                    "MIGRATION: If creating a domain, review the parent's content inventory below. "
                    "Identify covenants, files, and procedure sections that are SPECIFIC to the new "
                    "domain and should move there. Use semantic understanding, not just name matching. "
                    "'Stay in character during roleplay' belongs in a D&D domain even if it doesn't "
                    "say 'D&D'. Return IDs/names in the migrate_* arrays. Leave empty arrays if nothing to migrate."
                ),
                user_content=(
                    f"Current space: {space.name} (depth={space.depth})\n"
                    f"Existing spaces:\n" + ("\n".join(existing) or "(none)") + "\n\n"
                    f"Compaction summary:\n{doc[:3000]}\n\n"
                    f"Parent content (for migration if creating domain):\n{_parent_inventory}"
                ),
                output_schema=self.DOMAIN_ASSESSMENT_SCHEMA,
                max_tokens=512,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)

            # Handle explicit rename (independent of domain creation)
            if parsed.get("rename") and parsed.get("new_name", "").strip():
                new_name_rename = parsed["new_name"].strip()
                old_name = space.name
                aliases = list(space.aliases)
                if old_name and old_name not in aliases:
                    aliases.append(old_name)
                await self.state.update_context_space(tenant_id, space_id, {
                    "name": new_name_rename,
                    "aliases": aliases,
                    "renamed_from": old_name,
                    "renamed_at": utc_now(),
                })
                logger.info("DOMAIN_RENAME: space=%s old=%s new=%s evidence=%r",
                    space_id, old_name, new_name_rename, parsed.get("rename_evidence", ""))

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

            # Check for duplicate or drift (similar name to existing)
            new_name = parsed.get("name", "").strip()
            if not new_name:
                return
            for s in all_spaces:
                if s.name.lower() == new_name.lower() or new_name.lower() in [a.lower() for a in s.aliases]:
                    logger.info("DOMAIN_ASSESS: space=%s result=duplicate name=%s existing=%s", space_id, new_name, s.id)
                    return
                # Drift detection: similar but not identical name
                all_names = [s.name.lower()] + [a.lower() for a in s.aliases]
                if _is_similar_topic(new_name, all_names):
                    logger.info("DOMAIN_DRIFT: assessed=%s matches=%s (%s) — skipping creation",
                        new_name, s.name, s.id)
                    return

            # Enforce space cap
            await self._enforce_space_cap(tenant_id)

            now = utc_now()
            new_space = ContextSpace(
                id=f"space_{_uuid.uuid4().hex[:8]}",
                tenant_id=tenant_id,
                name=new_name,
                description=parsed.get("description", ""),
                posture=parsed.get("posture", ""),
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

            # Content migration: move LLM-identified domain-specific content
            try:
                await self._migrate_domain_content(
                    tenant_id, space_id, new_space.id, parsed)
            except Exception as mig_exc:
                logger.warning("DOMAIN_MIGRATE: failed for %s: %s", new_space.id, mig_exc)

        except Exception as exc:
            logger.warning("DOMAIN_ASSESS: failed for space=%s: %s", space_id, exc)

    async def _migrate_domain_content(
        self, tenant_id: str, parent_id: str, child_id: str,
        migrate_lists: dict,
    ) -> None:
        """Migrate domain-specific content from parent to child using LLM-selected lists.

        The domain assessment LLM identified which covenants, files, and procedure
        sections belong in the new domain. This method executes those moves.
        """
        migrated: dict[str, list[str]] = {"covenants": [], "files": [], "procedures": []}

        # 1. Migrate covenants by ID
        cov_ids = migrate_lists.get("migrate_covenants", [])
        for cov_id in cov_ids:
            try:
                await self.state.update_contract_rule(tenant_id, cov_id, {"context_space": child_id})
                migrated["covenants"].append(cov_id)
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: covenant %s failed: %s", cov_id, exc)

        # 2. Migrate procedure sections by title
        section_titles = set(migrate_lists.get("migrate_procedure_sections", []))
        if section_titles:
            try:
                parent_procs = await self._files.read_file(tenant_id, parent_id, "_procedures.md")
                if parent_procs and not parent_procs.startswith("Error:"):
                    sections = parent_procs.split("\n## ")
                    keep: list[str] = []
                    move: list[str] = []
                    for i, section in enumerate(sections):
                        full = ("## " + section) if i > 0 else section
                        title = section.split("\n")[0].strip().lstrip("# ").strip()
                        if title in section_titles or f"## {title}" in section_titles:
                            move.append(full)
                            migrated["procedures"].append(title)
                        else:
                            keep.append(full)
                    if move:
                        await self._files.write_file(
                            tenant_id, child_id, "_procedures.md",
                            "\n\n".join(move), "Domain procedures migrated from parent")
                        remaining = "\n\n".join(s for s in keep if s.strip())
                        if remaining.strip():
                            await self._files.write_file(
                                tenant_id, parent_id, "_procedures.md", remaining,
                                "Procedures (domain-specific sections migrated)")
                        else:
                            await self._files.delete_file(tenant_id, parent_id, "_procedures.md")
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: procedure migration failed: %s", exc)

        # 3. Migrate files by name
        file_names = migrate_lists.get("migrate_files", [])
        for fname in file_names:
            try:
                if fname.startswith("_") or fname.startswith("."):
                    continue
                content = await self._files.read_file(tenant_id, parent_id, fname)
                if content and not content.startswith("Error:"):
                    manifest = await self._files.load_manifest(tenant_id, parent_id)
                    desc = manifest.get(fname, "Migrated from parent")
                    await self._files.write_file(tenant_id, child_id, fname, content, desc)
                    await self._files.delete_file(tenant_id, parent_id, fname)
                    migrated["files"].append(fname)
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: file %s failed: %s", fname, exc)

        total = sum(len(v) for v in migrated.values())
        if total > 0:
            logger.info("DOMAIN_MIGRATE: space=%s from=%s covenants=%d procedures=%d files=%d",
                child_id, parent_id, len(migrated["covenants"]),
                len(migrated["procedures"]), len(migrated["files"]))
            for cat, items in migrated.items():
                for item in items:
                    logger.info("DOMAIN_MIGRATE_ITEM: type=%s item=%s action=moved", cat, item)

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

    # --- Downward search (CS-5) ---

    async def _downward_search(
        self, tenant_id: str, query: str, target_space_ids: list[str],
    ) -> str | None:
        """Search DOWN into child domains for an answer to a quick question."""
        import json as _json

        # Collect knowledge from target spaces and their children
        all_knowledge = await self.state.query_knowledge(
            tenant_id, active_only=True, limit=500)

        results_by_space: dict[str, list[str]] = {}
        for space_id in target_space_ids:
            space_ke = [
                k for k in all_knowledge
                if k.context_space == space_id
            ]
            # Also check children of this target
            children = await self.state.list_child_spaces(tenant_id, space_id)
            for child in children:
                space_ke.extend([k for k in all_knowledge if k.context_space == child.id])

            if space_ke:
                results_by_space[space_id] = [k.content for k in space_ke[:20]]

        if not results_by_space:
            logger.info("DOWNWARD_SEARCH_MISS: query=%r searched=%d found_in=none",
                query[:60], len(target_space_ids))
            return None

        # Use cheap model to resolve the answer
        space_names = {}
        for sid in results_by_space:
            s = await self.state.get_context_space(tenant_id, sid)
            space_names[sid] = s.name if s else sid

        context_parts = []
        for sid, facts in results_by_space.items():
            context_parts.append(f"From {space_names[sid]}:\n" + "\n".join(f"- {f}" for f in facts))

        try:
            answer = await self.reasoning.complete_simple(
                system_prompt=(
                    "Answer this question using ONLY the provided context from the user's "
                    "other domains. If you can answer, include which domain the answer came from. "
                    "If you can't answer from the context, say so briefly."
                ),
                user_content=(
                    f"Question: {query}\n\n"
                    + "\n\n".join(context_parts)
                ),
                max_tokens=256,
                prefer_cheap=True,
            )

            if answer and "can't answer" not in answer.lower() and "cannot answer" not in answer.lower():
                matched_spaces = list(results_by_space.keys())
                if len(matched_spaces) == 1:
                    logger.info("DOWNWARD_SEARCH_HIT: query=%r found_in=%s", query[:60], matched_spaces[0])
                else:
                    logger.info("DOWNWARD_SEARCH_HIT: query=%r found_in=%s", query[:60], matched_spaces)
                return f"[Quick answer from other context]\n{answer}"

            logger.info("DOWNWARD_SEARCH_MISS: query=%r searched=%d found_in=none",
                query[:60], len(target_space_ids))
            return None
        except Exception as exc:
            logger.warning("DOWNWARD_SEARCH: failed: %s", exc)
            return None

    # --- Cross-domain signals (CS-5) ---

    SIGNAL_ASSESSMENT_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "signal_worthy": {"type": "boolean"},
            "signal_text": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["signal_worthy", "signal_text", "reason"],
        "additionalProperties": False,
    }

    async def _check_cross_domain_signals(
        self, tenant_id: str, space_id: str,
        user_message: str, agent_response: str,
    ) -> None:
        """Post-turn check for cross-domain entity mentions with meaningful updates."""
        import json as _json

        if not user_message.strip():
            return

        # Get all knowledge entries
        all_knowledge = await self.state.query_knowledge(
            tenant_id, active_only=True, limit=500)

        # Build scope chain for current space
        from kernos.kernel.retrieval import RetrievalService
        _rs = RetrievalService.__new__(RetrievalService)
        _rs.state = self.state
        current_chain = set(await _rs._build_scope_chain(tenant_id, space_id))

        # Find knowledge entries in OTHER domains that mention entities from this turn
        combined = f"{user_message} {agent_response}".lower()
        cross_matches: list[tuple[str, Any]] = []  # (entity_text, KnowledgeEntry)
        seen_spaces: set[str] = set()
        for ke in all_knowledge:
            if not ke.context_space or ke.context_space in current_chain or ke.context_space in ("", None):
                continue
            # Check if any entity from this knowledge appears in the turn
            # Use subject as the entity identifier
            if ke.subject and ke.subject != "user" and ke.subject.lower() in combined:
                if ke.context_space not in seen_spaces:
                    cross_matches.append((ke.subject, ke))
                    seen_spaces.add(ke.context_space)

        if not cross_matches:
            return

        logger.info("CROSS_DOMAIN_CHECK: entities=%s cross_matches=%d",
            [m[0] for m in cross_matches], len(cross_matches))

        # Assess worthiness with cheap model
        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Determine if this conversation turn contains a MEANINGFUL UPDATE "
                    "about the named entity — a status change, new commitment, factual update, "
                    "or schedule change. Casual mentions, questions, or references without "
                    "new information are NOT signal-worthy."
                ),
                user_content=(
                    f"User: {user_message[:500]}\n"
                    f"Agent: {agent_response[:500]}\n\n"
                    f"Entities found in other domains: {[m[0] for m in cross_matches]}"
                ),
                output_schema=self.SIGNAL_ASSESSMENT_SCHEMA,
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)

            if not parsed.get("signal_worthy"):
                logger.info("CROSS_DOMAIN_SKIP: entities=%s reason=%r",
                    [m[0] for m in cross_matches], parsed.get("reason", ""))
                return

            signal_text = parsed.get("signal_text", "")
            if not signal_text:
                return

            # Get current space name for attribution
            current_space = await self.state.get_context_space(tenant_id, space_id)
            source_name = current_space.name if current_space else space_id

            for entity_name, ke in cross_matches:
                notice_text = f"[From {source_name}] {signal_text}"
                await self.state.append_space_notice(
                    tenant_id, ke.context_space, notice_text,
                    source=space_id, notice_type="cross_domain",
                )
                logger.info("CROSS_DOMAIN_SIGNAL: target=%s source=%s signal=%s",
                    ke.context_space, space_id, notice_text[:80])

        except Exception as exc:
            logger.warning("CROSS_DOMAIN_CHECK: assessment failed: %s", exc)

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

                        # Tier 3: Promote successfully used tools into local affordance set
                        if primary_ctx.active_space and primary_ctx.tool_calls_trace:
                            asyncio.ensure_future(self._promote_used_tools(
                                primary_ctx.tenant_id, primary_ctx.active_space_id,
                                primary_ctx.active_space, primary_ctx.tool_calls_trace))
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

        # Default: list all spaces (user-facing — no internal fields)
        from datetime import datetime, timezone
        spaces = await self.state.list_context_spaces(tenant_id)
        active = [s for s in spaces if s.status == "active"]
        if not active:
            return "No context spaces found."

        now = datetime.now(timezone.utc)
        lines = ["**Your Spaces**\n"]
        for s in sorted(active, key=lambda x: x.last_active_at or "", reverse=True):
            if s.space_type == "system":
                continue  # Don't show system internals
            current = " **(you are here)**" if s.id == ctx.active_space_id else ""
            default = " — default" if s.is_default else ""
            # Relative time
            age = ""
            if s.last_active_at:
                try:
                    last = datetime.fromisoformat(s.last_active_at)
                    days = (now - last).days
                    if days == 0:
                        age = " — active today"
                    elif days == 1:
                        age = " — yesterday"
                    elif days < 7:
                        age = f" — {days} days ago"
                    else:
                        age = f" — {days}d ago"
                except (ValueError, TypeError):
                    pass
            parent_note = ""
            if s.parent_id:
                parent = next((p for p in active if p.id == s.parent_id), None)
                if parent:
                    parent_note = f" (within {parent.name})"
            lines.append(f"- **{s.name}**{current}{default}{parent_note}{age}")
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

        # Query mode: quick question about another domain — stay in current space
        if ctx.router_result.query_mode and current_focus_id and ctx.router_result.focus != current_focus_id:
            target_space_ids = [
                t for t in ctx.router_result.tags
                if t != current_focus_id and not t.startswith("_")
            ]
            if target_space_ids:
                logger.info("DOWNWARD_SEARCH: query=%r target_domains=%s",
                    (message.content or "")[:60], target_space_ids)
                answer = await self._downward_search(
                    tenant_id, message.content or "", target_space_ids)
                if answer:
                    if ctx.results_prefix:
                        ctx.results_prefix += f"\n\n{answer}"
                    else:
                        ctx.results_prefix = answer
            # Stay in current space regardless
            ctx.router_result = RouterResult(
                tags=ctx.router_result.tags,
                focus=current_focus_id,
                continuation=False,
                query_mode=True,
            )

        # Work mode: intentional domain-specific work — route there confidently
        if ctx.router_result.work_mode and current_focus_id and ctx.router_result.focus != current_focus_id:
            logger.info("WORK_MODE: routing to %s for domain-specific work",
                ctx.router_result.focus)

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


        ctx.active_space = (
            await self.state.get_context_space(tenant_id, ctx.active_space_id)
            if ctx.active_space_id else None
        )
        if ctx.active_space and ctx.active_space_id:
            await self.state.update_context_space(tenant_id, ctx.active_space_id,
                {"last_active_at": utc_now(), "status": "active"})
            # Lazy workspace registration — ensure built tools are in the catalog
            try:
                await self._workspace.ensure_registered(tenant_id, ctx.active_space_id)
            except Exception as exc:
                logger.warning("WORKSPACE: lazy registration failed for %s: %s", ctx.active_space_id, exc)

            # Lazy catalog version promotion — scan for new tools relevant to this space
            try:
                await self._check_catalog_version(tenant_id, ctx.active_space_id, ctx.active_space)
            except Exception as exc:
                logger.warning("CATALOG_VERSION: check failed for %s: %s", ctx.active_space_id, exc)

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
        space_messages, ctx.results_prefix, ctx.memory_prefix, _procedures_prefix = await self._assemble_space_context(
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

        # --- Cohort agents: Message Analyzer + Covenant Query -------------------
        # Single LLM call replaces separate Preference Parser + Knowledge Shaper.
        # Four-way classification: preference | procedure | action | conversation.

        MESSAGE_ANALYSIS_SCHEMA = {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": ["preference", "procedure", "action", "conversation"],
                    "description": (
                        "What kind of message is this? "
                        "'preference' = short behavioral rule (auto-capture as covenant). "
                        "'procedure' = multi-step workflow instructions (write to _procedures.md). "
                        "'action' = user wants something done. "
                        "'conversation' = chat, question, or continuation."
                    ),
                },
                "preference": {
                    "type": "object",
                    "properties": {
                        "detected": {"type": "boolean"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "category": {"type": "string"},
                        "subject": {"type": "string"},
                        "action": {"type": "string"},
                        "parameters": {"type": "object"},
                        "scope_hint": {"type": "string"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["detected", "confidence", "category", "subject", "action", "parameters", "scope_hint", "reasoning"],
                    "additionalProperties": False,
                },
                "relevant_knowledge_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of knowledge entries relevant to this turn.",
                },
            },
            "required": ["classification", "preference", "relevant_knowledge_ids"],
            "additionalProperties": False,
        }

        async def _run_message_analysis() -> dict:
            """Combined message classification + knowledge selection + preference detection."""
            if _is_diagnostic or not user_content.strip():
                return {"classification": "conversation", "preference": {"detected": False, "confidence": "low", "category": "", "subject": "", "action": "", "parameters": {}, "scope_hint": "", "reasoning": ""}, "relevant_knowledge_ids": []}

            # Build knowledge candidates (Tier 1/2 filtering, same as before)
            all_ke = await self.state.query_knowledge(tenant_id, subject="user", active_only=True, limit=200)
            always_inject = [e for e in all_ke if e.lifecycle_archetype == "identity"]
            _never_archetypes = {"ephemeral"}
            candidates = [
                e for e in all_ke
                if e not in always_inject
                and e.lifecycle_archetype not in _never_archetypes
                and not getattr(e, "expired_at", "")
                and not (e.lifecycle_archetype == "contextual"
                         and _is_stale_knowledge(e, days=14))
            ]

            candidate_lines = "\n".join(
                f"- [{e.id}] \"{e.content}\" ({e.lifecycle_archetype})"
                for e in candidates
            ) if candidates else "(no candidates)"

            recent_context = self._get_recent_context_summary(ctx)

            try:
                import json as _json
                result_str = await self.reasoning.complete_simple(
                    system_prompt=(
                        "Analyze this message. Classify it, detect preferences, and select relevant knowledge.\n\n"
                        "Classification:\n"
                        "- 'preference': short behavioral rule like 'always do X' or 'never ask about Y'\n"
                        "- 'procedure': multi-step workflow like 'when I eat, log it, estimate, show budget'\n"
                        "- 'action': user wants something done\n"
                        "- 'conversation': chat, question, continuation\n\n"
                        "If preference detected: fill in the preference object with category, subject, action.\n"
                        "Select knowledge entry IDs relevant to answering this message. Return NONE-relevant as empty array."
                    ),
                    user_content=(
                        f"User message: \"{user_content[:300]}\"\n"
                        f"Recent context: {recent_context}\n\n"
                        f"Knowledge candidates:\n{candidate_lines}"
                    ),
                    output_schema=MESSAGE_ANALYSIS_SCHEMA,
                    max_tokens=256,
                    prefer_cheap=True,
                )
                parsed = _json.loads(result_str)
                logger.info("MESSAGE_ANALYSIS: classification=%s pref_detected=%s knowledge=%d",
                    parsed.get("classification", "?"),
                    parsed.get("preference", {}).get("detected", False),
                    len(parsed.get("relevant_knowledge_ids", [])))
                # Attach always_inject + shaped for downstream
                parsed["_always_inject"] = always_inject
                parsed["_candidates"] = candidates
                return parsed
            except Exception as exc:
                logger.warning("MESSAGE_ANALYSIS: failed: %s", exc)
                return {"classification": "conversation", "preference": {"detected": False, "confidence": "low", "category": "", "subject": "", "action": "", "parameters": {}, "scope_hint": "", "reasoning": ""}, "relevant_knowledge_ids": [], "_always_inject": always_inject, "_candidates": candidates}

        # Build scope chain for covenant inheritance (current + ancestors + global)
        _scope_chain = [active_space_id] if active_space_id else []
        if active_space and active_space.parent_id:
            _cur = active_space.parent_id
            _seen = {active_space_id}
            while _cur and _cur not in _seen:
                _scope_chain.append(_cur)
                _seen.add(_cur)
                _p = await self.state.get_context_space(tenant_id, _cur)
                _cur = _p.parent_id if _p and _p.parent_id else None
        space_scope = _scope_chain + [None] if _scope_chain else None

        # Fire Message Analyzer + Covenant Query in parallel
        analysis_result, contract_rules = await asyncio.gather(
            _run_message_analysis(),
            self.state.query_covenant_rules(
                tenant_id, context_space_scope=space_scope, active_only=True),
        )

        # Extract preference note (commit if detected)
        _pref = analysis_result.get("preference", {})
        if _pref.get("detected") and _pref.get("confidence") in ("high", "medium"):
            ctx.pref_detected = True
            try:
                from kernos.kernel.preference_parser import commit_from_analysis
                pref_note = await commit_from_analysis(
                    _pref, user_content, tenant_id, active_space_id,
                    self.state, self.reasoning,
                    getattr(self.reasoning, '_trigger_store', None),
                )
                if pref_note:
                    if ctx.results_prefix:
                        ctx.results_prefix += "\n\n" + pref_note
                    else:
                        ctx.results_prefix = pref_note
            except Exception as exc:
                logger.warning("PREF_COMMIT: failed: %s", exc)

        # Extract knowledge entries
        _relevant_ids = set(analysis_result.get("relevant_knowledge_ids", []))
        _always = analysis_result.get("_always_inject", [])
        _cands = analysis_result.get("_candidates", [])
        shaped = [e for e in _cands if e.id in _relevant_ids]
        user_knowledge_entries = _always + shaped

        # --- Three-tier tool surfacing (TOOL-SURFACING-REDESIGN) ----------------
        from kernos.kernel.reasoning import REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL, MANAGE_CAPABILITIES_TOOL
        from kernos.kernel.awareness import DISMISS_WHISPER_TOOL
        from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES, TOOL_TOKEN_BUDGET, SURFACER_SCHEMA

        # Build the kernel tool schema map (needed for all tiers)
        _kernel_tool_map: dict[str, dict] = {}
        from kernos.kernel.files import FILE_TOOLS
        from kernos.kernel.reasoning import READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL
        from kernos.kernel.covenant_manager import MANAGE_COVENANTS_TOOL
        from kernos.kernel.channels import MANAGE_CHANNELS_TOOL, SEND_TO_CHANNEL_TOOL
        from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
        from kernos.kernel.tools import INSPECT_STATE_TOOL
        from kernos.kernel.code_exec import EXECUTE_CODE_TOOL
        from kernos.kernel.workspace import MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL
        _all_kernel = FILE_TOOLS + [REQUEST_TOOL, READ_DOC_TOOL, DISMISS_WHISPER_TOOL,
                                MANAGE_CAPABILITIES_TOOL, REMEMBER_DETAILS_TOOL,
                                READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL,
                                MANAGE_COVENANTS_TOOL, MANAGE_CHANNELS_TOOL,
                                SEND_TO_CHANNEL_TOOL, MANAGE_SCHEDULE_TOOL,
                                INSPECT_STATE_TOOL, EXECUTE_CODE_TOOL,
                                MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL]
        if self._retrieval:
            from kernos.kernel.retrieval import REMEMBER_TOOL
            _all_kernel.append(REMEMBER_TOOL)
        for t in _all_kernel:
            _kernel_tool_map[t["name"]] = t

        # === BUDGETED TOOL WINDOW (SPEC-TOOL-WINDOW) ===
        # Two zones: PINNED (always loaded) + ACTIVE (token-budgeted, LRU eviction)

        def _schema_tokens(schema: dict) -> int:
            return len(json.dumps(schema)) // 4

        # --- Zone 1: PINNED (always loaded, never evicted) ---
        pinned_tools: list[dict] = []
        _added: set[str] = set()

        def _add_tool(schema: dict) -> bool:
            name = schema.get("name", "")
            if name and name not in _added:
                _added.add(name)
                return True
            return False

        for name in ALWAYS_PINNED:
            if name in _kernel_tool_map:
                if _add_tool(_kernel_tool_map[name]):
                    pinned_tools.append(_kernel_tool_map[name])
        # remember is pinned if available
        if self._retrieval and "remember" in _kernel_tool_map:
            if _add_tool(_kernel_tool_map["remember"]):
                pinned_tools.append(_kernel_tool_map["remember"])

        _pinned_tokens = sum(_schema_tokens(t) for t in pinned_tools)

        # --- Zone 2: ACTIVE (token-budgeted, schema-weighted LRU) ---
        active_budget = TOOL_TOKEN_BUDGET - _pinned_tokens
        _tier = "common"

        # Collect candidate tools with priority scores
        # Priority: lower = keep longer. Schema-weighted LRU.
        _affordance = {}
        if active_space and isinstance(active_space.local_affordance_set, dict):
            _affordance = active_space.local_affordance_set
        _turn = getattr(self, '_turn_counter', 0)
        self._turn_counter = _turn + 1

        candidates: list[tuple[dict, int]] = []  # (schema, eviction_priority)

        # Session-loaded tools get priority (recently used this session)
        loaded_names = self.reasoning.get_loaded_tools(active_space_id)

        # Common MCP tools get low priority score (preferred to keep)
        for name in COMMON_MCP_NAMES:
            if name in _added:
                continue
            schema = self.registry.get_tool_schema(name)
            if schema and _add_tool(schema):
                tokens = _schema_tokens(schema)
                candidates.append((schema, tokens))  # low priority = keep

        # Local affordance set tools
        for name, meta in _affordance.items():
            if name in _added:
                continue
            schema = (_kernel_tool_map.get(name)
                      or self.registry.get_tool_schema(name)
                      or self._load_workspace_tool_schema(tenant_id, name))
            if schema and _add_tool(schema):
                tokens = _schema_tokens(schema)
                turns_unused = max(1, _turn - meta.get("last_turn", 0))
                candidates.append((schema, turns_unused * tokens))

        # Session-loaded tools
        for name in loaded_names:
            if name in _added:
                continue
            schema = self.registry.get_tool_schema(name)
            if schema and _add_tool(schema):
                tokens = _schema_tokens(schema)
                candidates.append((schema, tokens))  # recently loaded = low priority

        # Space-activated capabilities (via request_tool)
        if active_space and active_space.active_tools:
            for cap_name in active_space.active_tools:
                cap = self.registry.get(cap_name)
                if cap and cap.tools:
                    for tname in cap.tools:
                        if tname in _added:
                            continue
                        schema = self.registry.get_tool_schema(tname)
                        if schema and _add_tool(schema):
                            candidates.append((schema, _schema_tokens(schema)))

        # Tier 2: Catalog scan for this turn's intent
        _msg_text = (message.content or "").strip()
        _unsurfaced = self._tool_catalog.get_names() - _added
        if _msg_text and len(_msg_text) > 5 and _unsurfaced:
            catalog_text = self._tool_catalog.build_catalog_text(exclude=_added)
            if catalog_text:
                try:
                    import json as _json
                    scan_result = await self.reasoning.complete_simple(
                        system_prompt=(
                            "Given the user's message, select which additional tools from the catalog "
                            "are needed. Only select tools directly relevant. Return empty array if "
                            "the loaded tools are sufficient.\n\n"
                            f"Already loaded: {sorted(_added)}"
                        ),
                        user_content=f"User message: \"{_msg_text[:300]}\"\n\nTool catalog:\n{catalog_text}",
                        output_schema=SURFACER_SCHEMA,
                        max_tokens=128,
                        prefer_cheap=True,
                    )
                    parsed_scan = _json.loads(scan_result)
                    scan_tools = parsed_scan.get("tools", [])
                    if scan_tools:
                        _tier = "catalog_scan"
                        for tool_name in scan_tools:
                            if tool_name in _added:
                                continue
                            # Try kernel → MCP → workspace descriptor
                            schema = _kernel_tool_map.get(tool_name) or self.registry.get_tool_schema(tool_name)
                            if not schema:
                                schema = self._load_workspace_tool_schema(tenant_id, tool_name)
                            if schema and _add_tool(schema):
                                tokens = _schema_tokens(schema)
                                candidates.append((schema, 0))  # scan-selected = highest priority
                                self.reasoning.load_tool(active_space_id, tool_name)
                        logger.info("TOOL_SURFACING: tier=catalog_scan selected=%s", scan_tools)
                except Exception as exc:
                    logger.warning("TOOL_SURFACING: catalog scan failed: %s", exc)

        # Sort candidates by eviction priority (ascending = keep first)
        candidates.sort(key=lambda x: x[1])

        # Fill active zone within budget
        active_tools: list[dict] = []
        _active_tokens = 0
        _evicted: list[str] = []
        for schema, priority in candidates:
            tokens = _schema_tokens(schema)
            if _active_tokens + tokens <= active_budget:
                active_tools.append(schema)
                _active_tokens += tokens
            else:
                _evicted.append(schema.get("name", "?"))

        # Assemble final tool list: pinned first (sorted), then active (sorted)
        pinned_tools.sort(key=lambda t: t.get("name", ""))
        active_tools.sort(key=lambda t: t.get("name", ""))
        tools = pinned_tools + active_tools

        _total_tokens = _pinned_tokens + _active_tokens
        _total = len(self._tool_catalog.get_names())
        if _evicted:
            logger.info("TOOL_EVICT: evicted=%s", _evicted)
        logger.info("TOOL_BUDGET: total=%d pinned=%d active=%d tokens=%d/%d evicted=%d",
            len(tools), len(pinned_tools), len(active_tools),
            _total_tokens, TOOL_TOKEN_BUDGET, len(_evicted))
        logger.info("TOOL_SURFACING: tier=%s surfaced=%d total_available=%d",
            _tier, len(tools), _total)
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

        # Build space name map for covenant attribution
        _space_names: dict[str, str] = {}
        if active_space:
            _space_names[active_space_id] = active_space.name
        for sid in _scope_chain:
            if sid not in _space_names:
                _s = await self.state.get_context_space(tenant_id, sid)
                if _s:
                    _space_names[sid] = _s.name

        rules = _build_rules_block(PRIMARY_TEMPLATE, contract_rules, soul, space_names=_space_names)
        now_block = _build_now_block(message, soul, active_space)
        state_block = _build_state_block(soul, PRIMARY_TEMPLATE, user_knowledge_entries)
        results = _build_results_block(ctx.results_prefix)
        actions = _build_actions_block(capability_prompt, message, self._channel_registry)
        memory = _build_memory_block(ctx.memory_prefix)
        procedures = _build_procedures_block(_procedures_prefix)

        # Cache boundary: static prefix (RULES + ACTIONS) is stable across turns,
        # dynamic suffix (NOW + STATE + RESULTS + PROCEDURES + MEMORY) changes every turn.
        ctx.system_prompt_static = _compose_blocks(rules, actions)
        ctx.system_prompt_dynamic = _compose_blocks(now_block, state_block, results, procedures, memory)
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

        # Cross-domain signal check — async, non-blocking
        try:
            import asyncio as _aio
            _aio.create_task(self._check_cross_domain_signals(
                ctx.tenant_id, ctx.active_space_id,
                ctx.message.content or "", ctx.response_text))
        except Exception:
            pass

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
                                    # Fact harvest is now integrated into the compaction call
                                    comp_state = await self.compaction.compact_from_log(
                                        tenant_id, ctx.active_space_id, ctx.active_space, log_text, log_num, comp_state)
                                    old_num, new_num = await self.conv_logger.roll_log(tenant_id, ctx.active_space_id)
                                    _seed = comp_state.last_seed_depth
                                    _seed_source = "adaptive" if _seed != 10 else "default"
                                    await self.conv_logger.seed_from_previous(tenant_id, ctx.active_space_id, old_num, tail_entries=_seed)
                                    logger.info("COMPACTION_SEED: space=%s depth=%d (%s)",
                                        ctx.active_space_id, _seed, _seed_source)
                                    self.reasoning.clear_loaded_tools(ctx.active_space_id)
                                    comp_state.consecutive_failures = 0
                                    comp_state.last_compaction_failure_at = ""
                                    logger.info("COMPACTION_COMPLETE: space=%s source=log_%03d new_log=log_%03d",
                                        ctx.active_space_id, old_num, new_num)

                                    # Process fact harvest from compaction output
                                    _harvest = getattr(comp_state, '_fact_harvest', [])
                                    if _harvest:
                                        try:
                                            from kernos.kernel.fact_harvest import process_harvest_results
                                            await process_harvest_results(
                                                _harvest, tenant_id, ctx.active_space_id,
                                                self.state, self.events)
                                        except Exception as _hx:
                                            logger.warning("COMPACTION_HARVEST: processing failed: %s", _hx)

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

    def _load_workspace_tool_schema(self, tenant_id: str, tool_name: str) -> dict | None:
        """Load a workspace tool's schema from its .tool.json descriptor."""
        catalog_entry = self._tool_catalog.get(tool_name)
        if not catalog_entry or catalog_entry.source != "workspace":
            return None
        home_space = getattr(catalog_entry, "home_space", "")
        if not home_space:
            return None
        # Find the descriptor file from the workspace manifest
        try:
            desc_file = f"{tool_name}.tool.json"
            from kernos.utils import _safe_name
            desc_path = (
                Path(os.getenv("KERNOS_DATA_DIR", "./data"))
                / _safe_name(tenant_id) / "spaces" / home_space / "files" / desc_file
            )
            if desc_path.exists():
                descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
                return {
                    "name": descriptor.get("name", tool_name),
                    "description": descriptor.get("description", ""),
                    "input_schema": descriptor.get("input_schema", {"type": "object", "properties": {}}),
                }
        except Exception as exc:
            logger.warning("WORKSPACE_SCHEMA_LOAD: failed for %s: %s", tool_name, exc)
        return None

    async def _check_catalog_version(
        self, tenant_id: str, space_id: str, space: ContextSpace,
    ) -> None:
        """Lazy version promotion: scan new tools for relevance to this space.

        On space entry, if catalog.version > space.last_catalog_version,
        new tools have been registered since last visit. Run a cheap LLM
        check to see if any are relevant, and promote them into the
        space's local affordance set.
        """
        import json as _json
        catalog = self._tool_catalog
        if not catalog or space.last_catalog_version >= catalog.version:
            return  # Up to date

        # Get tools not already in this space's affordance set
        aff = space.local_affordance_set if isinstance(space.local_affordance_set, dict) else {}
        current_set = set(aff.keys())
        from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES
        already_known = current_set | ALWAYS_PINNED | COMMON_MCP_NAMES
        new_tools = [
            e for e in catalog.get_all()
            if e.name not in already_known and e.source == "workspace"
        ]

        if not new_tools:
            # No new workspace tools — just update the version marker
            await self.state.update_context_space(tenant_id, space_id, {
                "last_catalog_version": catalog.version,
            })
            return

        # Ask cheap LLM: which of these new tools are relevant to this space?
        tool_lines = "\n".join(f"- {t.name}: {t.description}" for t in new_tools)
        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Given this context space and the new tools below, which tools "
                    "would be regularly useful in this domain? Only include tools that "
                    "are genuinely relevant to this space's typical work. Return a JSON "
                    "array of tool names, or an empty array if none are relevant."
                ),
                user_content=(
                    f"Space: {space.name}\n"
                    f"Description: {space.description}\n\n"
                    f"New tools:\n{tool_lines}"
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "promote": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["promote"],
                    "additionalProperties": False,
                },
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)
            to_promote = [n for n in parsed.get("promote", []) if n in {t.name for t in new_tools}]

            if to_promote:
                new_aff = dict(aff)
                for name in to_promote:
                    new_aff[name] = {"last_turn": 0, "tokens": 0}
                await self.state.update_context_space(tenant_id, space_id, {
                    "local_affordance_set": new_aff,
                    "last_catalog_version": catalog.version,
                })
                logger.info("TOOL_CATALOG_SCAN: space=%s new_tools=%d promoted=%d tools=%s",
                    space_id, len(new_tools), len(to_promote), to_promote)
            else:
                await self.state.update_context_space(tenant_id, space_id, {
                    "last_catalog_version": catalog.version,
                })
                logger.info("TOOL_CATALOG_SCAN: space=%s new_tools=%d promoted=0",
                    space_id, len(new_tools))
        except Exception as exc:
            # On failure, still update version to avoid re-scanning every turn
            logger.warning("TOOL_CATALOG_SCAN: failed for %s: %s", space_id, exc)
            await self.state.update_context_space(tenant_id, space_id, {
                "last_catalog_version": catalog.version,
            })

    async def _promote_used_tools(
        self, tenant_id: str, space_id: str, space: ContextSpace, tool_trace: list[dict],
    ) -> None:
        """Tier 3: Promote successfully used tools into the space's local affordance set.

        Updates last_turn for already-promoted tools. General (default root) only
        promotes universal tools — domain-specific tools should trigger routing.
        """
        from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES
        try:
            aff = dict(space.local_affordance_set) if isinstance(space.local_affordance_set, dict) else {}
            _turn = getattr(self, '_turn_counter', 0)
            changed = False
            for call in tool_trace:
                name = call.get("name", "")
                if not name or not call.get("success"):
                    continue
                # Skip pinned tools (they're always loaded)
                if name in ALWAYS_PINNED or name in COMMON_MCP_NAMES:
                    continue
                # General space guard
                if space.is_default and self.registry:
                    is_universal = False
                    for cap in self.registry.get_all():
                        if name in (cap.tools or []) and getattr(cap, "universal", False):
                            is_universal = True
                            break
                    if not is_universal:
                        catalog_entry = self._tool_catalog.get(name)
                        if catalog_entry and not catalog_entry.source.startswith("kernel"):
                            logger.info("TOOL_PROMOTE_SKIP: tool=%s space=%s reason=general_guard",
                                name, space_id)
                            continue
                # Compute schema tokens for this tool
                schema = self.registry.get_tool_schema(name)
                tokens = len(json.dumps(schema)) // 4 if schema else 0
                if name in aff:
                    aff[name]["last_turn"] = _turn
                    changed = True
                else:
                    aff[name] = {"last_turn": _turn, "tokens": tokens}
                    changed = True
                    logger.info("TOOL_PROMOTED: tool=%s space=%s reason=successful_use", name, space_id)
            if changed:
                await self.state.update_context_space(tenant_id, space_id, {
                    "local_affordance_set": aff,
                })
                for t in promoted:
                    logger.info("TOOL_PROMOTED: tool=%s space=%s reason=successful_use", t, space_id)
        except Exception as exc:
            logger.warning("TOOL_PROMOTE: failed: %s", exc)

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
            recent_topic = self._get_recent_context_summary(ctx)

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

    def _get_recent_context_summary(self, ctx: TurnContext) -> str:
        """Extract a brief summary of recent conversation for knowledge shaping."""
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
