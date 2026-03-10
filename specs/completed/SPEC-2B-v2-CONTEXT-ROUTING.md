# SPEC-2B: Context Space Routing (v2)

**Status:** READY FOR IMPLEMENTATION
**Depends on:** SPEC-2A (complete), ContextSpace model (planted)
**Replaces:** The previous 2B (algorithmic router, alias/entity matching). The algorithmic router is removed. The ContextSpace model, State Store additions, posture/rule injection, and CLI survive.
**Design source:** Context Routing Design Spec v0.3 (founder + Kit)

**Objective:** Route messages to the right space using an LLM that reads language. Maintain per-space conversation threads. Assemble context so the agent sees a coherent domain conversation, not a flat interleaved stream.

**What changes for the user:** Nothing visible. The user just talks. But the agent's responses improve because it sees a coherent thread for each domain instead of every topic mixed together.

**What changes architecturally:** Every message is tagged by a lightweight LLM router. Conversation history is reconstructed per-space from tagged messages. The agent receives a space thread plus ephemeral cross-domain injections. Spaces create themselves organically through two gates (algorithmic count + LLM judgment).

-----

## Component 1: Tagged Message Stream

**Modified file:** `kernos/persistence/json_file.py` (ConversationStore)

Every message stored gains a `space_tags` field — a list of space IDs this message belongs to. Tags are assigned by the router, stored on the message record, never shown to the main agent.

### Message record evolution

Current message format:
```json
{"role": "user", "content": "I attack the ogre!", "timestamp": "..."}
```

New format:
```json
{
  "role": "user",
  "content": "I attack the ogre! Oh — call John when we're done.",
  "timestamp": "2026-03-10T14:32:00Z",
  "space_tags": ["space_dnd_veloria", "space_5b632b42"]
}
```

The `space_tags` field is a list because a message can belong to multiple spaces simultaneously. "I attack the ogre! Oh — call John when we're done." is tagged D&D + Daily.

Existing untagged messages (from pre-2B conversations) default to `["daily_space_id"]` on load.

### Thread reconstruction

The ConversationStore gains a method to reconstruct a space-specific thread:

```python
async def get_space_thread(
    self, tenant_id: str, conversation_id: str,
    space_id: str, max_messages: int = 50,
) -> list[dict]:
    """Return messages tagged to this space, in chronological order.

    Filters the full message stream by space_tags containing space_id.
    Returns the most recent max_messages that match.
    """
    all_messages = await self.get_recent(tenant_id, conversation_id, limit=500)
    space_messages = [
        m for m in all_messages
        if space_id in m.get("space_tags", [])
    ]
    return space_messages[-max_messages:]
```

### Cross-domain injection

```python
async def get_cross_domain_messages(
    self, tenant_id: str, conversation_id: str,
    active_space_id: str, last_n_turns: int = 5,
) -> list[dict]:
    """Return recent messages from OTHER spaces for ephemeral injection.

    Returns the last N message pairs (user + assistant) that were NOT
    tagged to the active space. Includes both sides of the exchange.
    """
    all_messages = await self.get_recent(tenant_id, conversation_id, limit=last_n_turns * 4)
    cross = [
        m for m in all_messages
        if active_space_id not in m.get("space_tags", [])
    ]
    return cross[-last_n_turns * 2:]  # N turns = ~2N messages (user + assistant)
```

-----

## Component 2: The LLM Router

**New file:** `kernos/kernel/router.py` (replaces the previous algorithmic router)

A Haiku-class LLM call on every message. Cheap, fast, bounded. It reads language because routing requires understanding meaning.

### What the router sees

```python
class LLMRouter:
    """Route messages to context spaces using a lightweight LLM.

    The router reads the message, recent history, space descriptions,
    and temporal metadata. It tags the message to one or more spaces
    and decides which space gets the main agent's focus.

    Uses complete_simple() with structured output.
    """

    def __init__(self, state: StateStore, reasoning: ReasoningService):
        self.state = state
        self.reasoning = reasoning
```

The router's context window contains:

1. **Active spaces** — name + description for each
2. **Recent message history** — last 10-15 messages with their existing space tags
3. **Temporal metadata** — current time, gap since last message, approximate session duration
4. **The new message**

### Router prompt

```python
ROUTER_SYSTEM_PROMPT = """You are a message router. Given the user's message, recent conversation history, and a list of context spaces, do three things:

1. TAG: Which space(s) does this message belong to? A message can belong to multiple spaces. Use space IDs from the list.

2. FOCUS: Which single space should the agent focus on right now? When in doubt, choose Daily. The cost of defaulting to Daily is low — if the domain continues, it reasserts next message.

3. CONTINUATION: Is this an obvious continuation (short affirmation, reaction, "lol", "ok") that should ride conversational momentum? If yes, keep the current focus unchanged.

Rules:
- When a message signals something NEW within an existing domain ("new campaign", "starting fresh", "not the old one"), tag Daily. Let the new topic accumulate before it earns a space.
- Ambiguity is not a domain signal. When uncertain, Daily.
- A message mentioning a person or entity from one domain doesn't mean the message IS about that domain. "Henderson plays D&D" while chatting casually is Daily, not Business.
- Read the message in the context of recent history. A message after a 12-hour gap is a fresh start. A message 30 seconds after the last one is a continuation.
"""
```

### Router output schema

```python
ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Space IDs this message belongs to"
        },
        "focus": {
            "type": "string",
            "description": "Space ID for the main agent's focus"
        },
        "continuation": {
            "type": "boolean",
            "description": "True if this is an obvious continuation riding momentum"
        }
    },
    "required": ["tags", "focus", "continuation"],
    "additionalProperties": False
}
```

### Router call

```python
async def route(self, tenant_id: str, message: NormalizedMessage,
                recent_history: list[dict]) -> RouterResult:

    spaces = await self.state.list_context_spaces(tenant_id)
    if not spaces or len(spaces) == 1:
        daily = next((s for s in spaces if s.is_default), None)
        daily_id = daily.id if daily else ""
        return RouterResult(tags=[daily_id], focus=daily_id, continuation=False)

    # Build space list for the prompt
    space_descriptions = "\n".join([
        f"- {s.id}: {s.name} — {s.description or 'No description yet'}"
        for s in spaces if s.status == "active"
    ])

    # Build recent history with timestamps and existing tags
    history_lines = []
    for msg in recent_history[-15:]:
        ts = msg.get("timestamp", "")
        role = msg.get("role", "")
        content = msg.get("content", "")[:200]  # Truncate long messages
        tags = msg.get("space_tags", [])
        tag_names = self._resolve_tag_names(tags, spaces)
        history_lines.append(f"[{ts}] ({role}) [{', '.join(tag_names)}]: {content}")

    # Temporal metadata
    now = _now_iso()
    last_msg_time = recent_history[-1].get("timestamp", "") if recent_history else ""
    gap = _compute_gap_description(last_msg_time, now)

    user_content = (
        f"Active spaces:\n{space_descriptions}\n\n"
        f"Recent history:\n" + "\n".join(history_lines) + "\n\n"
        f"Time context: {now}. Gap since last message: {gap}.\n\n"
        f"New message: {message.content}"
    )

    result = await self.reasoning.complete_simple(
        system_prompt=ROUTER_SYSTEM_PROMPT,
        user_content=user_content,
        output_schema=ROUTER_SCHEMA,
        max_tokens=128,
        prefer_cheap=True,  # Haiku-class
    )

    parsed = json.loads(result)
    return RouterResult(
        tags=parsed.get("tags", [daily_id]),
        focus=parsed.get("focus", daily_id),
        continuation=parsed.get("continuation", False),
    )
```

### RouterResult

```python
@dataclass
class RouterResult:
    tags: list[str]       # Space IDs this message belongs to
    focus: str            # Space ID for main agent focus
    continuation: bool    # Obvious continuation — ride momentum
```

### Cost

One Haiku call per message. ~$0.001 per call. At 50 messages/day: $0.05/day, ~$1.50/month. The router context is small (space list + 15 messages + the new message) — well within Haiku's efficient range.

-----

## Component 3: Space Thread Assembly

**Modified file:** `kernos/messages/handler.py`

When the main agent processes a message, it receives a space-specific context window — not the flat interleaved stream.

### Assembly structure

```python
# Token budget for space threads — tunable parameter, not hardcoded.
# 4000 tokens ≈ 50-60 message exchanges before truncation.
# Starting conservative. Adjust based on real usage data — longer threads
# may need 6000-8000 tokens depending on conversation depth and model context limits.
SPACE_THREAD_TOKEN_BUDGET = 4000

CROSS_DOMAIN_INJECTION_TURNS = 5  # Also tunable. Fixed turns, not time-based.

The main agent's message array is built as:

```python
async def _assemble_space_context(
    self, tenant_id: str, conversation_id: str,
    active_space_id: str, token_budget: int = SPACE_THREAD_TOKEN_BUDGET,
) -> tuple[list[dict], str | None]:
    """Assemble the agent's conversation context for the active space.

    Returns (message_list, system_prefix) where:
    - message_list: the space thread (coherent domain conversation)
    - system_prefix: cross-domain injection text to prepend to system prompt (or None)
    """
    messages = []

    # 1. Cross-domain injections — last 5 turns from other spaces
    cross = await self.conversations.get_cross_domain_messages(
        tenant_id, conversation_id, active_space_id, last_n_turns=5
    )
    if cross:
        # Format as a system message prefix — NOT a synthetic user/assistant exchange.
        # The agent sees this as background awareness, not as something it said.
        injection_text = "Recent activity in other areas:\n"
        for msg in cross:
            role = "You" if msg["role"] == "assistant" else "User"
            ts = msg.get("timestamp", "")
            content = msg["content"][:300]
            injection_text += f"[{role}, {ts}]: {content}\n"
        injection_text += "\n(Above is recent context from other conversations. Current thread follows.)"

        # Inject as system-level context, not as fake dialogue
        system_prefix = injection_text
    else:
        system_prefix = None

    # 2. Space thread — the coherent domain conversation
    thread = await self.conversations.get_space_thread(
        tenant_id, conversation_id, active_space_id,
        max_messages=50,
    )

    # Simple truncation for now — oldest messages drop off
    # Compaction (smarter selection of what stays) is future work
    truncated = self._truncate_to_budget(thread, token_budget)
    messages.extend(truncated)

    return messages, system_prefix
```

### Ephemeral injection format

Cross-domain messages are injected at the **top** of the context window. Top placement means lower attention weight — the agent is aware of what else happened but the space thread dominates.

Injections include both user and agent messages. If the agent said something relevant in D&D that matters to Legal, Legal sees it.

Injections do NOT persist in the space thread. They appear once, for context, and fade away after a few turns.

The agent never sees space tags. Tags are router metadata only.

### Simple truncation (pre-compaction)

```python
def _truncate_to_budget(self, messages: list[dict], budget_tokens: int) -> list[dict]:
    """Simple oldest-first truncation to fit within token budget.

    Phase 2: just drop oldest messages until we fit.
    Phase 3: smart compaction (summarize old messages, keep high-value ones).
    """
    # Rough token estimation: 4 chars ≈ 1 token
    total = sum(len(m.get("content", "")) // 4 for m in messages)

    while total > budget_tokens and len(messages) > 2:
        dropped = messages.pop(0)
        total -= len(dropped.get("content", "")) // 4

    return messages
```

-----

## Component 4: Posture + Rule Injection

**Survives from previous 2B, unchanged.**

When a non-daily space is active, the system prompt gains the space's posture paragraph and all covenant rules scoped to that space.

```python
if active_space and not active_space.is_default and active_space.posture:
    parts.append(
        f"## Current operating context: {active_space.name}\n"
        f"(This shapes your working style — it does not override "
        f"your core values or hard boundaries.)\n"
        f"{active_space.posture}"
    )

rules = await self.state.query_covenant_rules(
    tenant_id,
    context_space_scope=[active_space_id, None],
    active_only=True,
)
if rules:
    parts.append(_format_covenant_rules(rules))
```

-----

## Component 5: Space Creation (Two Gates)

### Gate 1 — Algorithmic: Tag Count Threshold

The router tags every message. The kernel tracks how many messages have been tagged toward unnamed topic clusters. When a cluster crosses a threshold, Gate 2 fires.

**Implementation:** After each routing call, if any tags point to topics that don't match an existing space, increment a counter in a lightweight tracking structure. The tracking structure is a dict in the State Store: `{topic_hint: message_count}`.

The router can signal emerging topics by tagging with a descriptive hint instead of a space ID. For example, tagging "legal_work" when no Legal space exists. The kernel counts these hints.

```python
# In handler, after routing:
for tag in router_result.tags:
    if not await self.state.get_context_space(tenant_id, tag):
        # Not a known space — it's a topic hint
        await self.state.increment_topic_hint(tenant_id, tag)
        count = await self.state.get_topic_hint_count(tenant_id, tag)
        if count >= SPACE_CREATION_THRESHOLD:  # TBD — err high
            await self._trigger_gate2(tenant_id, tag)
```

**Threshold:** Set high. Spaces are earned. A number like 15-20 messages about the same topic before Gate 2 even fires. Daily is a fine home for most things.

### Gate 2 — LLM: Judgment Call

One LLM call. It sees the accumulated messages tagged toward this topic and answers:

1. Is this a real recurring domain worth its own space, or a one-off that ran long?
2. If yes: what's the best name? Write an initial description.

```python
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

async def _trigger_gate2(self, tenant_id: str, topic_hint: str):
    # Gather messages tagged with this hint
    tagged_messages = await self._get_messages_for_hint(tenant_id, topic_hint)

    result = await self.reasoning.complete_simple(
        system_prompt=(
            "You are evaluating whether a topic in someone's life deserves "
            "its own dedicated context space. A space is for recurring domains "
            "— ongoing projects, hobbies, professional areas — not one-off "
            "topics that happened to run long. If this is a real domain, "
            "name it concisely and write a 1-2 sentence description."
        ),
        user_content=f"Messages about this topic:\n{self._format_messages(tagged_messages)}",
        output_schema=GATE2_SCHEMA,
        max_tokens=256,
        prefer_cheap=True,
    )

    parsed = json.loads(result)
    if parsed.get("create_space"):
        # LRU Sunset: enforce active space cap before creating
        await self._enforce_space_cap(tenant_id)

        new_space = ContextSpace(
            id=f"space_{uuid4().hex[:8]}",
            tenant_id=tenant_id,
            name=parsed["name"],
            description=parsed["description"],
            space_type="domain",
            status="active",
            created_at=_now_iso(),
            last_active_at=_now_iso(),
            is_default=False,
        )
        await self.state.save_context_space(new_space)
        await self.events.emit(Event(
            type=EventType.CONTEXT_SPACE_CREATED,
            tenant_id=tenant_id,
            source="gate2",
            payload={"space_id": new_space.id, "name": new_space.name},
        ))
        # Retag the accumulated messages with the new space ID
        # (they were tagged with the topic hint — now point to the real space)
```

### LRU Sunset Cap

Hard cap on active spaces. When Gate 2 would create a new space and the cap is hit, the least recently used active space is archived automatically. No user prompt. No confirmation.

```python
ACTIVE_SPACE_CAP = 40  # Tunable — likely 30-50. Start at 40.

async def _enforce_space_cap(self, tenant_id: str):
    """Archive the least recently used space if at cap."""
    spaces = await self.state.list_context_spaces(tenant_id)
    active = [s for s in spaces if s.status == "active" and not s.is_default]

    if len(active) < ACTIVE_SPACE_CAP:
        return  # Under cap, nothing to do

    # Find LRU — oldest last_active_at among non-default active spaces
    lru = sorted(active, key=lambda s: s.last_active_at)[0]

    # Archive it — thread preserved on disk, removed from router's active list
    await self.state.update_context_space(tenant_id, lru.id, {
        "status": "archived",
    })
    await self.events.emit(Event(
        type=EventType.CONTEXT_SPACE_SUSPENDED,
        tenant_id=tenant_id,
        source="space_cap",
        payload={"space_id": lru.id, "name": lru.name, "reason": "lru_sunset"},
    ))
```

Archived spaces:
- Thread preserved on disk (messages still in the full stream with their tags)
- Removed from the router's active space list (not shown to the router LLM)
- Not deleted — can be manually reactivated via CLI or future UI
- The daily space is never archived (`is_default` exempt from LRU)

-----

## Component 6: Session Exit Maintenance

When focus shifts away from a non-daily space, one LLM call reviews the session and updates the space description. This is the single mechanism for spaces getting smarter about themselves over time.

```python
async def _run_session_exit(self, tenant_id: str, space_id: str):
    """Update space description based on what happened in this session."""

    space = await self.state.get_context_space(tenant_id, space_id)
    if not space or space.is_default:
        return  # Daily doesn't need session exit maintenance

    # Get messages from this session (since last focus switch to this space)
    session_messages = await self._get_session_messages(tenant_id, space_id)
    if len(session_messages) < 3:
        return  # Too short to update description

    result = await self.reasoning.complete_simple(
        system_prompt=(
            "Review this conversation session and update the space description. "
            "The description helps the router understand what this space is about. "
            "Rename the space if the session revealed something the name misses. "
            "Keep the description to 1-3 sentences."
        ),
        user_content=(
            f"Space: {space.name}\n"
            f"Current description: {space.description}\n\n"
            f"This session:\n{self._format_messages(session_messages)}"
        ),
        output_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["name", "description"],
            "additionalProperties": False
        },
        max_tokens=256,
        prefer_cheap=True,
    )

    parsed = json.loads(result)
    await self.state.update_context_space(tenant_id, space_id, {
        "name": parsed.get("name", space.name),
        "description": parsed.get("description", space.description),
    })
```

**Trigger:** Runs when the router's focus shifts away from a non-daily space. Also runs after meaningful inactivity (configurable threshold, e.g. 30 minutes) — session resets to Daily and exit maintenance fires for the last active space.

-----

## Component 7: ContextSpace Model Cleanup

**Modified file:** `kernos/kernel/spaces.py`

Remove dead fields from the algorithmic router. Add description.

```python
@dataclass
class ContextSpace:
    id: str
    tenant_id: str
    name: str
    description: str = ""            # NEW — router uses this for routing decisions
    space_type: str = "daily"        # "daily" | "project" | "domain" | "managed_resource"
    status: str = "active"           # "active" | "dormant" | "archived"
    posture: str = ""                # Plain English working style
    model_preference: str = ""       # Reserved Phase 3
    created_at: str = ""
    last_active_at: str = ""
    is_default: bool = False

    # REMOVED: routing_keywords, routing_entity_ids, routing_aliases
    # The LLM router reads descriptions and message content, not keyword lists.
    # REMOVED: suggestion_suppressed_until
    # Replaced by Gate 1/Gate 2 creation system.
```

**Migration:** Existing ContextSpace records with the old fields load correctly — unknown fields are silently ignored by the dataclass. The daily space created in the Schema Foundation Sprint keeps its fields. No data migration needed.

-----

## Component 8: State Store Additions

### Topic hint tracking (for Gate 1)

```python
# New methods on StateStore:

@abstractmethod
async def increment_topic_hint(self, tenant_id: str, hint: str) -> None:
    """Increment the message count for an unnamed topic cluster."""
    ...

@abstractmethod
async def get_topic_hint_count(self, tenant_id: str, hint: str) -> int:
    """Get current message count for a topic hint."""
    ...

@abstractmethod
async def clear_topic_hint(self, tenant_id: str, hint: str) -> None:
    """Clear a topic hint after space creation or expiration."""
    ...
```

Storage: `{data_dir}/{tenant_id}/state/topic_hints.json` — simple dict of `{hint: count}`.

### Surviving from previous 2B

- `query_covenant_rules` with `context_space_scope` filtering — already implemented
- `last_active_space_id` on TenantProfile — already implemented

-----

## Component 9: Handler Integration

The handler's `process()` flow changes:

```python
async def process(self, message: NormalizedMessage) -> str:
    tenant_id = derive_tenant_id(message)
    await self._ensure_tenant_state(tenant_id, message)

    # 1. Load recent history (flat, with tags)
    recent = await self.conversations.get_recent(tenant_id, conversation_id, limit=20)

    # 2. Route the message (LLM call)
    router_result = await self.router.route(tenant_id, message, recent)

    # 3. Tag the message in the stream
    # (tags are stored when the message is saved later)

    # 4. Detect space switch
    active_space_id = router_result.focus
    previous_space_id = tenant_profile.last_active_space_id

    if active_space_id != previous_space_id and previous_space_id:
        # Session exit maintenance on the outgoing space
        await self._run_session_exit(tenant_id, previous_space_id)
        # Update tracking
        tenant_profile.last_active_space_id = active_space_id
        await self.state.save_tenant_profile(tenant_profile)
        # Emit switch event
        await self.events.emit(...)

    # 5. Check Gate 1 for emerging topics
    for tag in router_result.tags:
        if not await self.state.get_context_space(tenant_id, tag):
            await self.state.increment_topic_hint(tenant_id, tag)
            count = await self.state.get_topic_hint_count(tenant_id, tag)
            if count >= SPACE_CREATION_THRESHOLD:
                await self._trigger_gate2(tenant_id, tag)

    # 6. Load active space for posture/rules
    active_space = await self.state.get_context_space(tenant_id, active_space_id)

    # 7. Assemble space-specific context
    space_context = await self._assemble_space_context(
        tenant_id, conversation_id, active_space_id
    )

    # 8. Build system prompt with posture + scoped rules
    system_prompt = self._build_system_prompt(soul, active_space, tenant_id)

    # 9. Reasoning (using space_context instead of flat history)
    # ... existing task engine / reasoning flow ...

    # 10. Save response with space tags
    await self.conversations.append(tenant_id, conversation_id, {
        "role": "assistant",
        "content": response,
        "timestamp": _now_iso(),
        "space_tags": router_result.tags,
    })

    # 11. Memory projectors (pass active_space_id for scoping)
    # ... existing flow ...

    return response
```

-----

## Implementation Order

1. ContextSpace model cleanup — remove dead fields, add description
2. Message record evolution — add space_tags field, migration for untagged messages
3. ConversationStore additions — get_space_thread, get_cross_domain_messages
4. LLM Router — new router.py replacing old algorithmic router
5. Space thread assembly — _assemble_space_context in handler
6. Handler integration — rewire process() flow
7. Topic hint tracking — State Store additions for Gate 1
8. Gate 2 — space creation LLM call
9. Session exit maintenance — description update on focus shift
10. Remove old router code — delete algorithmic routing logic, clean imports
11. Tests — router output parsing, thread reconstruction, cross-domain injection, Gate 1 counting, Gate 2 creation, session exit, tag migration

-----

## What Claude Code MUST NOT Change

- Entity resolution pipeline (2A)
- Fact dedup pipeline (2A)
- Tier 2 extraction pipeline (only active_space_id passthrough)
- CovenantRule schema
- Soul data model
- Template content
- Reasoning Service (only the router's complete_simple calls are new)
- The system prompt builder (only posture + rule injection, already working)

-----

## Acceptance Criteria

1. **Every message gets space tags.** After any message, the conversation store shows space_tags on the message record. Verified via direct inspection of conversation JSON.

2. **Multi-tagging works.** "I attack the ogre! Oh — call John when we're done" → tagged to both D&D and Daily. Verified.

3. **Space thread reconstruction works.** `get_space_thread(space_dnd)` returns only D&D-tagged messages in order. No Daily messages bleed in. Verified.

4. **Cross-domain injection works.** When in D&D, the agent's context includes recent Daily messages at the top. When in Daily, recent D&D messages appear at the top. Injections are from the last 5 turns. Verified.

5. **Injections don't persist.** Cross-domain injections from 10 messages ago are gone from the context. They don't accumulate. Verified.

6. **Router handles "Henderson plays D&D" correctly.** While in Daily, this message stays in Daily (not routed to Business just because Henderson is mentioned). Verified.

7. **Router handles obvious continuations.** "lol" after a D&D message stays in D&D. Verified.

8. **Router parks new-domain signals in Daily.** "I want to start a new campaign, not Veloria" → tagged Daily, not routed to existing D&D space. Verified.

9. **Gate 1 counts correctly.** Messages tagged toward an unnamed topic accumulate. Count increments. Threshold triggers Gate 2. Verified.

10. **Gate 2 creates a space when warranted.** After threshold, Gate 2 LLM judges "this is a real domain" → space created with name and description. Verified via `kernos-cli spaces`.

11. **Gate 2 declines one-off topics.** A billing dispute that ran for 15 messages but isn't a domain → Gate 2 says no. No space created. Verified.

12. **Session exit updates description.** Focus shifts from D&D to Daily → D&D's description gets updated based on what happened in the session. Verified via `kernos-cli spaces` showing updated description.

13. **Dead fields removed.** ContextSpace no longer has routing_keywords, routing_entity_ids, routing_aliases. Verified.

14. **Simple truncation works.** When a space thread exceeds the token budget, oldest messages drop off. The thread stays within budget. Verified.

15. **Posture + scoped rules still work.** Carried from previous 2B — posture appears for non-daily spaces, rules scope correctly. Verified.

16. **Daily-only tenants are unaffected.** A tenant with only the daily space → router returns Daily, no thread splitting, behavior identical to current. Verified.

17. **LRU sunset enforces cap.** When active spaces hit the cap and Gate 2 creates a new space, the least recently used non-default space is archived. Its thread is preserved on disk. It no longer appears in the router's space list. Daily is never archived. Verified.

18. **Cross-domain injection is system-level.** Injections appear as system context, not as synthetic user/assistant messages. The agent's conversation history contains no fabricated exchanges. Verified by inspecting the message array sent to the provider.

19. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Prerequisites
- KERNOS running on Discord
- Test tenant (existing or fresh)

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send: "Hey, how's it going?" | Routes to Daily. Message tagged daily. |
| 2 | Send: "I'm thinking about starting a D&D campaign with you" | Routes to Daily (no D&D space exists yet). Tag accumulates. |
| 3 | Send 15-20 more D&D-related messages | Tags accumulate toward D&D topic. Gate 1 threshold approaches. |
| 4 | Once Gate 1 fires → check Gate 2 | Gate 2 creates D&D space with name and description. `kernos-cli spaces` shows it. |
| 5 | Send: "The tavern keeper's name is Varek" | Routes to the new D&D space. Agent responds in-character. |
| 6 | Send: "Oh by the way, I need to call my dentist tomorrow" | Tagged D&D + Daily. Focus stays in D&D (or shifts to Daily — router decides based on context). Daily thread receives it. |
| 7 | Send: "Ok I'm done with D&D for now, what's for dinner?" | Focus shifts to Daily. Session exit fires — D&D description updates. |
| 8 | `kernos-cli spaces` | D&D space has updated description reflecting what happened in the session. |
| 9 | Send: "What were we talking about in the campaign?" | Router routes to D&D. Agent sees the D&D thread — a coherent campaign conversation. Recent Daily messages injected at top as context. |
| 10 | Inspect conversation JSON | Messages have space_tags. D&D messages tagged to D&D space. Cross-tagged messages appear in both threads. |

Write results to `tests/live/LIVE-TEST-2B-v2.md`.

After live verification: update DECISIONS.md and docs/TECHNICAL-ARCHITECTURE.md.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| LLM router, not algorithmic | Routing requires reading meaning | Keyword matching fails on exactly the messages that matter. "Henderson plays D&D" can't be routed by entity ownership. A Haiku classifier reading context is a cheap bounded call, not general intelligence in the kernel. |
| Per-message LLM call | $0.001/message, ~$1.50/month | The cost of wrong routing (knowledge scoped to wrong domain, wrong posture, wrong rules) exceeds the cost of the call. |
| Spaces are conversation threads | Not just knowledge scopes | The agent sees a coherent domain conversation. This is fundamentally better than a flat interleaved stream with knowledge filtering. |
| Multi-tagging | Messages can belong to multiple spaces | "I attack the ogre! Call John when we're done" is both D&D and Daily. No forced choice, no lost information. |
| Cross-domain injection at top | Lower attention weight, doesn't persist | The agent is aware of what else happened but the space thread dominates. Injections fade naturally. |
| Fixed turns for injection (5) | Not time-based | Recency of conversation, not recency in time. Simpler, predictable, tunable. |
| Simple truncation pre-compaction | Oldest drops off at budget | Honest, predictable, doesn't pretend to be smart. Compaction refines this in a later phase. |
| Two-gate space creation | Algorithmic count + LLM judgment | High threshold means spaces are earned. Gate 2 prevents one-off topics from crystallizing. |
| Session exit updates descriptions | Spaces get smarter over time | The router uses descriptions. Richer descriptions = better routing. Single mechanism, runs once per exit. |
| Dead fields removed | No routing_keywords/aliases/entity_ids | The LLM router reads descriptions and content, not keyword lists. Dead fields create confusion. |
| LRU sunset cap | Archive least recently used space when at cap | Hard cap prevents unbounded space growth. Archived spaces are preserved on disk, removed from router's active list. No user prompt. Daily is exempt. |
| Cross-domain injection as system message | Not synthetic user/assistant dialogue | Putting words in the agent's mouth risks it referencing a conversation that never happened. System-level context is background awareness, not fake dialogue. |
| Token budget is a tunable parameter | Start at 4000, adjust from usage | 4000 tokens ≈ 50-60 exchanges. Conservative starting point. Easy to increase once there's real data on conversation depth. |
| Tags invisible to agent | Router metadata only | Tags would be noise in the conversation. The agent sees clean threads. |
| Temporal metadata in router context | Time of day, gap, session duration | A message at 2am after 12 hours is different from mid-afternoon. Language can't carry this signal. |
