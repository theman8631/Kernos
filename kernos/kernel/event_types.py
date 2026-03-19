"""Event type definitions for the KERNOS event stream.

Hierarchical type strings enable filtered subscriptions: "message.*" or "tool.*"
without parsing payloads.
"""
from enum import Enum


class EventType(str, Enum):
    # Message lifecycle
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"

    # Reasoning (LLM calls)
    REASONING_REQUEST = "reasoning.request"
    REASONING_RESPONSE = "reasoning.response"

    # Tool usage
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"

    # Tenant lifecycle
    TENANT_PROVISIONED = "tenant.provisioned"

    # Capability changes
    CAPABILITY_CONNECTED = "capability.connected"
    CAPABILITY_DISCONNECTED = "capability.disconnected"
    CAPABILITY_ERROR = "capability.error"

    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    # Agent lifecycle
    AGENT_HATCHED = "agent.hatched"
    AGENT_BOOTSTRAP_GRADUATED = "agent.bootstrap_graduated"

    # Knowledge
    KNOWLEDGE_EXTRACTED = "knowledge.extracted"

    # System
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    HANDLER_ERROR = "handler.error"

    # --- Phase 2: Covenant lifecycle (Pillar B) ---
    COVENANT_EVALUATED = "covenant.evaluated"
    COVENANT_ACTION_STAGED = "covenant.action.staged"
    COVENANT_ACTION_APPROVED = "covenant.action.approved"
    COVENANT_ACTION_REJECTED = "covenant.action.rejected"
    COVENANT_ACTION_EXPIRED = "covenant.action.expired"
    COVENANT_RULE_GRADUATED = "covenant.rule.graduated"
    COVENANT_RULE_REGRESSED = "covenant.rule.regressed"
    COVENANT_RULE_CREATED = "covenant.rule.created"
    COVENANT_RULE_UPDATED = "covenant.rule.updated"
    COVENANT_RULE_MERGED = "covenant.rule.merged"
    COVENANT_RULE_REPLACED = "covenant.rule.replaced"
    COVENANT_CONTRADICTION_DETECTED = "covenant.contradiction.detected"

    # --- Phase 2: Entity resolution (Pillar A) ---
    ENTITY_CREATED = "entity.created"
    ENTITY_MERGED = "entity.merged"
    ENTITY_LINKED = "entity.linked"

    # --- Phase 2: Knowledge lifecycle (Pillar A) ---
    KNOWLEDGE_REINFORCED = "knowledge.reinforced"
    KNOWLEDGE_INVALIDATED = "knowledge.invalidated"
    KNOWLEDGE_DECAYED = "knowledge.decayed"

    # --- Phase 2: Context Spaces ---
    CONTEXT_SPACE_CREATED = "context.space.created"
    CONTEXT_SPACE_SWITCHED = "context.space.switched"
    CONTEXT_SPACE_SUSPENDED = "context.space.suspended"

    # --- Phase 2C: Compaction ---
    COMPACTION_TRIGGERED = "compaction.triggered"
    COMPACTION_COMPLETED = "compaction.completed"
    COMPACTION_ROTATION = "compaction.rotation"

    # --- Phase 3D: Dispatch Interceptor ---
    DISPATCH_GATE = "dispatch.gate"
    # Payload: tool_name, effect, allowed, reason, method

    # --- Phase 3B+: MCP Installation ---
    TOOL_INSTALLED = "tool.installed"
    # Payload: capability_name, tool_count, universal
    TOOL_UNINSTALLED = "tool.uninstalled"
    # Payload: capability_name

    # --- Phase 3C: Proactive Awareness ---
    PROACTIVE_INSIGHT = "proactive.insight"
    # Payload: whisper_id, insight_text, delivery_class, source_space_id,
    #          target_space_id, knowledge_entry_id, reasoning_trace
