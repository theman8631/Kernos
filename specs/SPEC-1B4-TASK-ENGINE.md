# SPEC-1B4: Task Engine (Minimal)

**Status:** READY FOR IMPLEMENTATION
**Depends on:** 1B.2 (Reasoning Service), 1B.3 (Capability Registry) — complete
**Objective:** Formalize the concept of “a unit of work” with lifecycle tracking. Every piece of work in the system — from a simple message to a multi-step build — enters through the Task Engine. For 1B.4, only reactive-simple tasks exist: user sends message, engine executes reasoning, result comes back. The structure supports future task types (proactive, generative) without building them.

**Why this matters now, not later:**

Right now the handler does everything: message routing, provisioning, context assembly, reasoning execution, result persistence. There’s no concept of “work” separate from “message processing.” This means:

- There’s no place to inject behavioral contract checks before execution
- There’s no way for non-message sources to trigger work (awareness evaluator, daemon)
- There’s no task prioritization (user messages should preempt background work)
- There’s no lifecycle tracking (how long did this work take? what was its status?)

The Task Engine creates the entry point through which ALL work flows. For now it’s a pass-through for simple messages. But it’s the hook point for everything in Phase 2.

**Zero-cost-path:** For reactive-simple tasks (100% of current traffic), the engine is one function call wrapping the existing reasoning flow. No routing logic, no decomposition, no overhead beyond creating a Task dataclass and emitting two events.

-----

## Component 1: Task Data Model

**New file:** `kernos/kernel/task.py`

```python
from dataclasses import dataclass, field
from enum import Enum


class TaskType(str, Enum):
    REACTIVE_SIMPLE = "reactive_simple"       # User message, pass-through to reasoning
    # Future types — defined now so the enum is stable:
    # REACTIVE_COMPLEX = "reactive_complex"   # Multi-capability routing
    # PROACTIVE_ALERT = "proactive_alert"     # Time-sensitive notification
    # PROACTIVE_INSIGHT = "proactive_insight"  # Non-urgent insight delivery
    # GENERATIVE = "generative"               # Multi-step, multi-agent
    # THINK = "think"                         # Holistic reasoning, don't decompose


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
    is a Task with lifecycle tracking.
    """
    id: str                           # Unique, sortable: "task_{ts_us}_{rand4}"
    type: TaskType
    tenant_id: str
    conversation_id: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = TaskPriority.USER_INTERACTIVE

    # Lifecycle timestamps (ISO 8601)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    # Input
    source: str = "user_message"     # What triggered this task
    input_text: str = ""              # The user's message (for reactive tasks)

    # Result (populated on completion)
    result_text: str = ""             # The response
    error_message: str = ""           # If failed, what went wrong

    # Metrics (populated on completion)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0
    tool_iterations: int = 0


def generate_task_id() -> str:
    """Generate a unique, time-sortable task ID."""
    import time
    import uuid
    ts_us = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"task_{ts_us}_{rand}"
```

**Key decisions:**

- `Task` is a mutable dataclass — status and result fields are updated during lifecycle. Events capture the immutable history; the Task object is the working state.
- Priority uses integer levels (like Unix nice values). Lower = higher priority. Constants are named for readability but any integer works. This supports future priority queuing without enum rigidity.
- `source` tracks what created the task. Currently always `"user_message"`. Future: `"awareness_evaluator"`, `"consolidation_daemon"`, `"task_engine"` (subtask decomposition).
- Metrics (tokens, cost, duration) are copied from ReasoningResult on completion. One place to find “how much did this task cost.”
- Future types are commented in the enum, not defined. This documents intent without creating dead code.

-----

## Component 2: Task Engine

**New file:** `kernos/kernel/engine.py`

```python
class TaskEngine:
    """Routes work through the kernel.

    For 1B.4: reactive-simple pass-through only.
    Every message creates a Task, the engine executes it via ReasoningService,
    lifecycle events are emitted, result is returned.
    """

    def __init__(
        self,
        reasoning: ReasoningService,
        events: EventStream,
    ) -> None:
        self._reasoning = reasoning
        self._events = events

    async def execute(self, task: Task, request: ReasoningRequest) -> Task:
        """Execute a task. Returns the task with result populated.

        Currently handles only REACTIVE_SIMPLE: call reasoning, return result.
        Emits task.created and task.completed/task.failed events.

        The request contains pre-assembled context (messages, system_prompt, tools).
        Context assembly is the handler's job for now — it will migrate to the
        kernel's context assembly layer in Phase 2.
        """
        # Mark running
        task.status = TaskStatus.RUNNING
        task.started_at = _now_iso()

        # Emit task.created
        try:
            await emit_event(
                self._events,
                EventType.TASK_CREATED,
                task.tenant_id,
                "task_engine",
                payload={
                    "task_id": task.id,
                    "task_type": task.type.value,
                    "priority": task.priority,
                    "source": task.source,
                    "conversation_id": task.conversation_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit task.created: %s", exc)

        # Execute
        try:
            result = await self._reasoning.reason(request)

            # Populate task with result
            task.status = TaskStatus.COMPLETED
            task.completed_at = _now_iso()
            task.result_text = result.text
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            task.estimated_cost_usd = result.estimated_cost_usd
            task.duration_ms = result.duration_ms
            task.tool_iterations = result.tool_iterations

            # Emit task.completed
            try:
                await emit_event(
                    self._events,
                    EventType.TASK_COMPLETED,
                    task.tenant_id,
                    "task_engine",
                    payload={
                        "task_id": task.id,
                        "task_type": task.type.value,
                        "duration_ms": task.duration_ms,
                        "input_tokens": task.input_tokens,
                        "output_tokens": task.output_tokens,
                        "estimated_cost_usd": task.estimated_cost_usd,
                        "tool_iterations": task.tool_iterations,
                        "conversation_id": task.conversation_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit task.completed: %s", exc)

            return task

        except Exception as exc:
            # Any reasoning error — task failed
            task.status = TaskStatus.FAILED
            task.completed_at = _now_iso()
            task.error_message = str(exc)

            # Emit task.failed
            try:
                await emit_event(
                    self._events,
                    EventType.TASK_FAILED,
                    task.tenant_id,
                    "task_engine",
                    payload={
                        "task_id": task.id,
                        "task_type": task.type.value,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": task.conversation_id,
                    },
                )
            except Exception as emit_exc:
                logger.warning("Failed to emit task.failed: %s", emit_exc)

            # Re-raise so the handler can produce the appropriate error response
            raise
```

**Key decisions:**

- **The engine does NOT catch reasoning errors.** It emits `task.failed`, then re-raises. The handler still catches `ReasoningTimeoutError` etc. and produces user-friendly error messages. The engine tracks lifecycle; the handler owns user communication.
- **The engine takes a pre-assembled ReasoningRequest.** Context assembly (history loading, system prompt building) stays in the handler for now. The outline envisions the kernel assembling context, but that requires the context assembly/annotation layer from Phase 2. For 1B.4, the handler knows how to build context and the engine knows how to execute work.
- **`execute()` is synchronous-sequential.** No queue, no priority scheduling, no concurrent execution. When we need those (proactive tasks, background work), the engine gains an internal queue and a run loop. But for one-at-a-time reactive tasks, direct execution is correct.
- **`execute()` returns the Task**, not just the result text. The handler reads `task.result_text` and proceeds with its message lifecycle (persist, emit, summarize). This also lets the handler log task metadata if needed.

-----

## Component 3: Event Types

**Modified file:** `kernos/kernel/event_types.py`

Add three new event types:

```python
# Task lifecycle
TASK_CREATED = "task.created"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
```

-----

## Component 4: Handler Refactoring

**Modified file:** `kernos/messages/handler.py`

The handler gains the TaskEngine as a dependency and creates tasks instead of calling reasoning directly.

**Constructor changes:**

```python
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
    engine: TaskEngine,        # NEW
) -> None:
```

**process() changes:**

The middle section of `process()` changes from:

```python
request = ReasoningRequest(...)
try:
    result = await self.reasoning.reason(request)
    response_text = result.text
except ReasoningTimeoutError: ...
```

To:

```python
task = Task(
    id=generate_task_id(),
    type=TaskType.REACTIVE_SIMPLE,
    tenant_id=tenant_id,
    conversation_id=conversation_id,
    source="user_message",
    input_text=message.content,
    created_at=_now_iso(),
)

request = ReasoningRequest(...)
try:
    task = await self.engine.execute(task, request)
    response_text = task.result_text
except ReasoningTimeoutError: ...
```

**What stays the same in handler:**

- Tenant provisioning
- History loading
- message.received / message.sent events
- Conversation persistence (append user/assistant entries)
- Conversation summary updates
- Error handling (catch reasoning errors, produce friendly messages, emit handler.error)
- System prompt building (with capability_prompt from registry)

**What moves to the task engine:**

- The `await self.reasoning.reason(request)` call
- Task lifecycle tracking (created/completed/failed events)
- Cost/token/duration metrics aggregation at the task level

The handler still catches errors — the engine re-raises after emitting `task.failed`. The handler’s error handling produces the user-facing response and emits `handler.error`. Both events exist in the stream: `task.failed` for the engine’s view, `handler.error` for the handler’s view. They’re complementary, not redundant — the task event tracks work lifecycle, the handler event tracks message flow.

-----

## Component 5: Entry Point Wiring

**Modified files:** `kernos/app.py`, `kernos/discord_bot.py`

Both construct the TaskEngine and pass it to the handler.

```python
from kernos.kernel.engine import TaskEngine

# After reasoning service is created:
engine = TaskEngine(reasoning=reasoning, events=events)

handler = MessageHandler(
    mcp=mcp_manager,
    conversations=conversations,
    tenants=tenants,
    audit=audit,
    events=events,
    state=state,
    reasoning=reasoning,
    registry=registry,
    engine=engine,        # NEW
)
```

**Note:** The handler still takes `reasoning` as a parameter even though the engine also holds a reference. This is because the handler might need direct reasoning access for things that aren’t tasks (e.g., quick inline evaluations in the future). For now, only the engine uses it. If this feels redundant during implementation, the handler can drop its direct reasoning reference and access via `self.engine._reasoning` — but explicit is better.

-----

## Component 6: CLI Extension

**Modified file:** `kernos/cli.py`

Add a `tasks` subcommand:

```bash
./kernos-cli tasks <tenant_id> [--limit N]
```

Output:

```
────────────────────────────────────────────────────────────
  Tasks for discord_364303223047323649  (5 shown)
────────────────────────────────────────────────────────────
[2026-03-04 05:17:27] task_1772601447... reactive_simple COMPLETED (2439ms, $0.040)
[2026-03-04 05:16:14] task_1772601381... reactive_simple COMPLETED (1856ms, $0.036)
[2026-03-04 03:52:26] task_1772595146... reactive_simple COMPLETED (2067ms, $0.038)
```

Implementation: query event stream for `task.completed` and `task.failed` events, display summary.

-----

## Component 7: Housekeeping — CLI capabilities fix

**Modified file:** `kernos/cli.py`

Add `from dotenv import load_dotenv; load_dotenv()` near the top of the CLI module (after the existing imports). This fixes the `capabilities` command showing AVAILABLE instead of CONFIGURED for calendar — it wasn’t finding `GOOGLE_OAUTH_CREDENTIALS_PATH` because the CLI didn’t load `.env`.

-----

## File Structure

```
kernos/kernel/
├── __init__.py
├── engine.py              # NEW — TaskEngine
├── event_types.py         # Modified — add task.created/completed/failed
├── events.py              # Unchanged
├── exceptions.py          # Unchanged
├── reasoning.py           # Unchanged
├── state.py               # Unchanged
├── state_json.py          # Unchanged
└── task.py                # NEW — Task, TaskType, TaskStatus, TaskPriority
```

-----

## Acceptance Criteria

1. **Task dataclass** exists with all fields: id, type, tenant_id, conversation_id, status, priority, lifecycle timestamps, input_text, result_text, error_message, metrics.
1. **TaskType enum** includes REACTIVE_SIMPLE. Future types commented but not defined.
1. **TaskStatus enum** includes PENDING, RUNNING, COMPLETED, FAILED.
1. **TaskEngine.execute()** calls reasoning, populates task with result, emits task.created and task.completed events.
1. **TaskEngine.execute()** emits task.failed and re-raises on reasoning errors.
1. **Handler creates a Task** for every inbound message and calls `engine.execute()`.
1. **Handler no longer calls `self.reasoning.reason()` directly.** All reasoning goes through the engine.
1. **Event types** include task.created, task.completed, task.failed.
1. **CLI `tasks` command** shows task lifecycle from events.
1. **CLI `capabilities` command** fixed — loads .env, shows CONFIGURED when env vars present.
1. **All existing tests pass** — no regressions.
1. **Bot starts and responds to messages** — identical behavior to pre-1B.4.
1. **Event stream contains task events** alongside existing reasoning/message events.

-----

## Tests

**New file:** `tests/test_engine.py`

- Test TaskEngine.execute() with successful reasoning → task status COMPLETED, metrics populated
- Test TaskEngine.execute() with reasoning failure → task status FAILED, exception re-raised
- Test task.created event emitted with correct payload (task_id, type, priority, source)
- Test task.completed event emitted with correct payload (duration, tokens, cost)
- Test task.failed event emitted with correct payload (error_type, error_message)
- Test event emission failure doesn’t break execution (best-effort pattern)

**New file:** `tests/test_task.py`

- Test Task creation with defaults
- Test generate_task_id() produces sortable IDs
- Test TaskType and TaskStatus enum values

**Updated:** `tests/test_handler.py`

- Handler tests provide a mock TaskEngine
- Handler tests verify Task is created with correct type, tenant_id, conversation_id
- Handler tests verify result_text is extracted from completed task
- Handler tests verify reasoning errors are still caught and produce user-friendly messages

-----

## What 1B.4 deliberately does NOT build

- **Task queue / priority scheduling** — execute() is synchronous. Queue comes when background tasks exist.
- **Task decomposition** — generative tasks that break into subtasks. Phase 2.
- **Proactive task creation** — awareness evaluator injecting tasks. Phase 2.
- **Task persistence** — tasks exist in memory during execution and in the event stream after. No task store. If we need to query “active tasks” we read the event stream. When task volume warrants it, a TaskStore projection can be added.
- **Behavioral contract enforcement at task level** — the engine is the natural place for pre-execution contract checks (“is this action allowed?”). The hook point exists (before calling reasoning). The enforcement comes in a later spec.
- **Context assembly in the engine** — the outline envisions the kernel assembling context for agents. For now, the handler assembles context and passes it via ReasoningRequest. Migration happens when the context assembly layer is built.

-----

## Live Verification

**Live verification: REQUIRED**

### Step 1: Cold start

1. Restart the bot
1. Verify startup logs show TaskEngine initialization

### Step 2: Message flow with task events

1. Send: “Hey, how are you?”
1. Check events:
   
   ```bash
   ./kernos-cli events <tenant_id> --limit 10
   ```
1. Verify event sequence includes: message.received → task.created → reasoning.request → reasoning.response → task.completed → message.sent
1. Verify task.created has `task_type: "reactive_simple"` and `source: "user_message"`
1. Verify task.completed has duration_ms, tokens, cost

### Step 3: Task with tools

1. Send: “What’s on my schedule?”
1. Check events — should see task.created → reasoning chain with tools → task.completed
1. Verify task.completed cost includes all reasoning calls in the tool loop

### Step 4: CLI tasks command

1. Run: `./kernos-cli tasks <tenant_id>`
1. Verify completed tasks are listed with status, duration, cost

### Step 5: CLI capabilities fix

1. Run: `./kernos-cli capabilities`
1. Google Calendar should show as CONFIGURED (not AVAILABLE)

### Step 6: Regression

1. Test conversation memory, calendar queries, general chat
1. All identical to pre-1B.4

-----

*Spec ready for review. After approval, founder commits to specs/ and triggers Claude Code.*