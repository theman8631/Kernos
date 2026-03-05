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

    # System
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    HANDLER_ERROR = "handler.error"
