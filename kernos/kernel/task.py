"""Task data model — the unit of work in the KERNOS system.

Every piece of work, from answering a simple question to building a multi-step
plan, is represented as a Task with full lifecycle tracking.
"""
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskType(str, Enum):
    REACTIVE_SIMPLE = "reactive_simple"     # User message, pass-through to reasoning
    # Future types — defined when the engine gains routing logic:
    # REACTIVE_COMPLEX = "reactive_complex"   # Multi-capability routing
    # PROACTIVE_ALERT = "proactive_alert"     # Time-sensitive notification
    # PROACTIVE_INSIGHT = "proactive_insight" # Non-urgent insight delivery
    # GENERATIVE = "generative"              # Multi-step, multi-agent
    # THINK = "think"                        # Holistic reasoning, don't decompose


class TaskStatus(str, Enum):
    PENDING = "pending"       # Created, not yet executing
    RUNNING = "running"       # Actively executing
    COMPLETED = "completed"   # Finished successfully
    FAILED = "failed"         # Finished with error


class TaskPriority:
    """Priority levels. Lower number = higher priority.

    User interactions always preempt background work.
    """
    USER_INTERACTIVE = 1    # User sent a message, waiting for response
    PROACTIVE_ALERT = 2     # Time-sensitive notification (future)
    PROACTIVE_INSIGHT = 5   # Non-urgent insight (future)
    BACKGROUND = 8          # Daemon work, maintenance (future)


@dataclass
class Task:
    """A unit of work in the KERNOS system.

    Every piece of work — from answering "what time is it?" to building a website —
    is a Task with lifecycle tracking. Events capture the immutable history;
    the Task object is the mutable working state.
    """

    id: str
    type: TaskType
    instance_id: str
    conversation_id: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = TaskPriority.USER_INTERACTIVE

    # Lifecycle timestamps (ISO 8601)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    # Input
    source: str = "user_message"   # What triggered this task
    input_text: str = ""           # The user's message (for reactive tasks)

    # Result (populated on completion)
    result_text: str = ""          # The response
    error_message: str = ""        # If failed, what went wrong

    # Metrics (populated on completion — mirrored from ReasoningResult)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0
    tool_iterations: int = 0


def generate_task_id() -> str:
    """Generate a unique, time-sortable task ID.

    Format: task_{microseconds_since_epoch}_{4_random_hex_chars}
    Lexicographic order matches chronological order.
    """
    ts_us = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"task_{ts_us}_{rand}"
