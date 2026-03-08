# SPEC-2B: Context Space Routing

**Status:** READY FOR IMPLEMENTATION
**Depends on:** SPEC-2A Entity Resolution + Fact Dedup (complete), ContextSpace model planted (complete)
**Objective:** Activate context spaces. Messages route to the right space. The system prompt loads the right posture and rules for that space. Space switches get a one-line annotation.

**What changes for the user:**
Before: All conversation is one flat context. Behavioral rules are global. The agent has one mode.
After: Different domains of the user's life have different agent behavior. The business space has business rules and a professional posture. The D&D space has a creative posture. Routing happens invisibly — the user just talks.

**What changes architecturally:**
Three additions to the message processing pipeline: a router at the top that picks the active space, posture + rule injection in the system prompt builder, and a one-line handoff annotation on space switches. One new State Store query method. One new field on TenantProfile.

**Scope — what's in 2B and what's not:**
IN: Context space routing, posture injection, scoped rule injection, handoff annotation.
NOT IN: Dispatch Interceptor (Phase 3 — Kernos is reactive in Phase 2, the LLM's judgment plus soul principles handle compliance), retrieval layer (2C), notification queue (deferred), batch evaluation (unnecessary — tool-use loop is sequential).

-----

## Component 1: Context Space Router

**New file:** `kernos/kernel/router.py`

When a message arrives, the router picks which space owns it. Runs first — after tenant provisioning, before anything else.

**Intent:** Fast, algorithmic, no LLM calls. Right 80% of the time. When uncertain, defaults to most recently active space and sets a flag. 2C's automatic pre-message retrieval can correct the routing as a side effect of subject detection.

```python
class ContextSpaceRouter:
    """Route inbound messages to context spaces.

    Algorithmic only. No LLM calls. Two checks then a default:
    1. Space name or alias match in the message text
    2. Entity ownership — a mentioned entity belongs to a specific space
    3. Default: most recently active space

    Always produces an answer. Never asks the user.
    """

    def __init__(self, state: StateStore):
        self.state = state

    async def route(
        self, tenant_id: str, message_text: str
    ) -> tuple[str, bool]:
        """Return (context_space_id, confident).

        confident=False means the router fell to the default.
        2C's pre-message pass can use this flag to correct routing
        as a side effect of subject detection.
        """
        spaces = await self.state.list_context_spaces(tenant_id)
        if not spaces or len(spaces) == 1:
            # Only daily exists — no routing needed
            daily = next((s for s in spaces if s.is_default), None)
            return (daily.id if daily else ""), True

        text_lower = message_text.lower()

        # Check 1: Space name or alias match
        for space in spaces:
            if space.is_default:
                continue  # Daily doesn't win via name match
            triggers = [space.name.lower()] + [a.lower() for a in space.routing_aliases]
            for trigger in triggers:
                if trigger in text_lower:
                    return space.id, True

        # Check 2: Entity ownership
        # Extract potential entity names from the message (lightweight)
        # Check if any known entities with a context_space match
        entities = await self.state.query_entity_nodes(tenant_id, active_only=True)
        for entity in entities:
            if not entity.context_space:
                continue  # Global entity — doesn't route anywhere
            names_to_check = [entity.canonical_name.lower()] + [a.lower() for a in entity.aliases]
            for name in names_to_check:
                if name in text_lower:
                    return entity.context_space, True

        # Check 3: Default — most recently active space
        active_spaces = sorted(
            [s for s in spaces if s.status == "active"],
            key=lambda s: s.last_active_at,
            reverse=True,
        )
        default = active_spaces[0] if active_spaces else spaces[0]
        return default.id, False  # confident=False — router is guessing
```

### Handler integration

At the top of `handler.process()`, after tenant provisioning:

```python
# Route to context space
active_space_id, routing_confident = await self.router.route(tenant_id, message.content)

# Load the active space
active_space = await self.state.get_context_space(tenant_id, active_space_id)

# Detect space switch
previous_space_id = tenant_profile.last_active_space_id
space_switched = active_space_id != previous_space_id and previous_space_id != ""

# Update tenant profile
if active_space_id != previous_space_id:
    tenant_profile.last_active_space_id = active_space_id
    await self.state.save_tenant_profile(tenant_profile)

# Update space's last_active_at
if active_space:
    await self.state.update_context_space(tenant_id, active_space_id, {
        "last_active_at": _now_iso(),
        "status": "active",
    })

# Emit switch event if space changed
if space_switched:
    await self.events.emit(Event(
        type=EventType.CONTEXT_SPACE_SWITCHED,
        tenant_id=tenant_id,
        source="router",
        payload={
            "from_space": previous_space_id,
            "to_space": active_space_id,
            "confident": routing_confident,
        }
    ))
```

The `active_space_id` and `routing_confident` flag propagate to:
- System prompt builder (posture + rule injection)
- Memory projectors (KnowledgeEntry.context_space on write)
- Eventually 2C's pre-message pass (uses the flag to decide if routing correction is needed)

-----

## Component 2: Posture + Rule Injection

**Modified file:** `kernos/messages/handler.py` — specifically `_build_system_prompt()`

When a non-daily space is active, the system prompt gains the space's posture and all covenant rules scoped to that space. This is how domain-specific behavioral rules surface automatically — the invoice rule appears when you're in the business space because the space is active, not because something detected the word "invoice."

### Posture injection

```python
# In _build_system_prompt(), after the soul/personality section:

if active_space and not active_space.is_default and active_space.posture:
    parts.append(
        f"## Current operating context: {active_space.name}\n"
        f"(This shapes your working style — it does not override "
        f"your core values or hard boundaries.)\n"
        f"{active_space.posture}"
    )
```

The posture is plain English. "Creative collaboration mode. Match the playful, exploratory tone of worldbuilding." The LLM reads it and adjusts. No structured fields, no categories.

The label "(does not override your core values or hard boundaries)" prevents the posture from being interpreted as overriding the soul's operating principles.

### Scoped rule injection

Load covenant rules for the active space + global rules, and format them into the system prompt. This replaces the current global-only contract injection.

```python
# In _build_system_prompt(), replacing the current contract formatting:

# Load rules: space-scoped + global
rules = await self.state.query_covenant_rules(
    tenant_id,
    context_space_scope=[active_space_id, None],
    active_only=True,
)

# Format rules into the system prompt (same format as today, just scoped)
if rules:
    parts.append(_format_covenant_rules(rules))
```

The formatting function groups rules by type (must, must_not, preference, escalation) the same way the current contract formatter does. The only change is which rules are loaded — scoped + global instead of all.

**Daily space behavior:** When the daily space is active, `context_space_scope=[daily_space_id, None]` loads daily-scoped rules (if any) plus global rules. In practice, most rules are global and daily-scoped rules are rare. The agent sees the same rules it sees today.

-----

## Component 3: Handoff Annotation

When routing detects a space change, one line is prepended to the message the LLM sees.

```python
# In handler.process(), after routing and before building the system prompt:

if space_switched:
    previous_space = await self.state.get_context_space(tenant_id, previous_space_id)
    if previous_space:
        annotation = f"[Switched from: {previous_space.name}]"
        # Prepend to the message content the LLM will see
        annotated_content = f"{annotation}\n{message.content}"
    else:
        annotated_content = message.content
else:
    annotated_content = message.content
```

Space name only. No last topic, no summary, no LLM call. The agent knows what it was doing from its conversation history — the annotation just tells it the domain changed.

If there's no previous space (first message, or previous space was deleted), no annotation. The message passes through unmodified.

-----

## Component 4: State Store Additions

**Modified files:** `kernos/kernel/state.py`, `kernos/kernel/state_json.py`

### TenantProfile addition

```python
@dataclass
class TenantProfile:
    # ... existing fields ...
    last_active_space_id: str = ""  # NEW — tracks which space was active last
```

### New query method

```python
# StateStore ABC addition:

@abstractmethod
async def query_covenant_rules(
    self, tenant_id: str,
    capability: str | None = None,
    context_space_scope: list[str | None] | None = None,
    active_only: bool = True,
) -> list[CovenantRule]:
    """Query covenant rules with optional capability and context space filtering.

    context_space_scope: list of space IDs to include. None in the list
    means global rules. Example: ["space_abc", None] returns rules scoped
    to space_abc plus all global rules.

    If context_space_scope is None (not provided), returns all rules
    regardless of space scoping. Used by CLI and admin tools.
    """
    ...
```

### JsonStateStore implementation

```python
async def query_covenant_rules(self, tenant_id, capability=None,
                                context_space_scope=None, active_only=True):
    rules = await self._load_covenant_rules(tenant_id)

    if active_only:
        rules = [r for r in rules if r.active]
    if capability:
        rules = [r for r in rules if r.capability == capability or r.capability == "general"]
    if context_space_scope is not None:
        rules = [r for r in rules if r.context_space in context_space_scope]

    return rules
```

-----

## Component 5: Memory Projector Space Scoping

**Modified file:** `kernos/kernel/projectors/coordinator.py`

When Tier 2 extraction writes KnowledgeEntries, the `context_space` field is set based on the active space:

- If active space is daily: `context_space = None` (global — daily knowledge applies everywhere)
- If active space is any other space: `context_space = active_space_id` (scoped to that domain)

```python
# In the coordinator, when building KnowledgeEntry candidates:
if active_space and not active_space.is_default:
    candidate.context_space = active_space_id
else:
    candidate.context_space = None  # Global
```

**Exception for cross-domain facts:** The Tier 2 extraction prompt already classifies lifecycle archetypes. Facts classified as `identity` or `structural` about the user ("user got a new job", "user is stressed this week") should be global regardless of which space they were extracted in. These are about the person, not the domain.

```python
# Override: user-level facts are always global
if candidate.subject == "user" and candidate.lifecycle_archetype in ("identity", "structural"):
    candidate.context_space = None
```

-----

## Implementation Order

1. `last_active_space_id` field on TenantProfile
2. `query_covenant_rules` method with context_space_scope filtering
3. Context Space Router — new file, route() method
4. Handler integration — router call at top of process(), space switch detection, event emission
5. Posture injection in _build_system_prompt()
6. Scoped rule injection (replace current global-only contract loading)
7. Handoff annotation prepended to message on space switch
8. Memory projector space scoping on KnowledgeEntry writes
9. Tests — routing (alias match, entity match, default fallback), posture injection, scoped rule loading, handoff annotation, space switch events, knowledge entry scoping

-----

## What Claude Code MUST NOT Change

- Entity resolution pipeline (2A)
- Fact dedup pipeline (2A)
- Tier 2 extraction logic (only the context_space field on output entries changes)
- CovenantRule schema (only queried, not modified)
- Soul data model
- Template content
- Reasoning Service (no changes in 2B)

-----

## Acceptance Criteria

1. **Alias routing works.** Message containing a space alias routes to that space. Verified via `kernos-cli spaces` showing updated `last_active_at`.

2. **Entity routing works.** Message mentioning an entity owned by a space routes to that space. Verified the same way.

3. **Default fallback works.** Ambiguous message routes to most recently active space. `routing_confident` is False. Verified via the `context.space.switched` event payload.

4. **Posture injection works.** When a non-daily space is active, the system prompt includes its posture section with the "does not override core values" label. Verified by inspecting the assembled prompt (add logging or a test that checks prompt content).

5. **Scoped rules load correctly.** In the business space, business-scoped rules + global rules appear in the prompt. D&D-scoped rules do NOT appear. Verified by creating rules scoped to different spaces and checking which load.

6. **Handoff annotation appears on switch.** Switching from TTRPG to daily → the LLM's input includes `[Switched from: TTRPG — Aethoria Campaign]`. Verified via event payload or prompt inspection.

7. **No annotation when space doesn't change.** Two consecutive messages in the same space → no annotation. Verified.

8. **Knowledge entries scoped correctly.** Facts extracted while in the D&D space have `context_space = space_dnd_id`. Facts about the user ("user is tired") extracted in any space have `context_space = None` (global). Verified via `kernos-cli knowledge`.

9. **Daily space is the no-op path.** A tenant with only the daily space → router returns daily, no posture injection, global rules only, no annotation. Everything behaves exactly like Phase 1B. Verified.

10. **All existing tests pass.** New tests cover routing, injection, annotation, and scoping.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Prerequisites
- KERNOS running on Discord
- Test tenant with the daily space (auto-created)
- Create at least one additional space manually via State Store (or add a CLI command for space creation — see below)

### Setup: Create a test space

Claude Code should add a CLI command for creating spaces:

```bash
./kernos-cli create-space <tenant_id> --name "Test Project" --type project \
    --aliases "the project,test" --keywords "testing,experiment" \
    --posture "Focused and methodical. This is a test environment."
```

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send a normal message (no space keywords) | Routes to daily (most recently active). No annotation. No posture. Global rules only. |
| 2 | `kernos-cli spaces <tenant_id>` | Daily space shows updated last_active_at. |
| 3 | Send: "Let's work on the test project" | Routes to Test Project space (alias match: "test project"). |
| 4 | `kernos-cli spaces <tenant_id>` | Test Project space shows updated last_active_at. |
| 5 | Check agent's response tone/approach | Should reflect the test space posture ("focused and methodical"). |
| 6 | Send another message without space keywords | Stays in Test Project (most recently active). No annotation. |
| 7 | Send: "Actually, what's for dinner tonight?" | Routes back to daily (no project keywords, daily becomes most recently active OR direct fallback). Annotation: `[Switched from: Test Project]`. |
| 8 | `kernos-cli knowledge <tenant_id>` after several messages in each space | Entries from Test Project have context_space set. Entries about the user from any space are global (None). |
| 9 | Create a covenant rule scoped to Test Project space | Rule should appear in prompt when in Test Project, not when in daily. |

Write results to `tests/live/LIVE-TEST-2B.md` per the testing protocol.

After live verification: update DECISIONS.md and docs/TECHNICAL-ARCHITECTURE.md per the completion checklist.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Router is algorithmic only | No LLM call for routing | Fast, free, right 80% of the time. 2C's pre-message pass corrects the other 20% as a side effect. |
| Uncertain routing is flagged, not blocked | `confident=False` propagates | The router always picks a space. If it's uncertain, downstream (2C) can correct. Never asks the user. |
| Posture is plain English with boundary label | Trust the model, clarify scope | The LLM reads a paragraph and adjusts. Explicit note that posture doesn't override core values. |
| Rules scoped via query, not separate storage | One covenant rule store, filtered by space | No new data structure. Same CovenantRule records, just filtered by context_space on load. |
| Handoff annotation is space name only | No last_topic, no summary | The agent has conversation history. It knows what it was doing. The annotation just says the domain changed. One-line addition later if needed. |
| Daily space is the zero-cost path | Single-space tenants behave exactly like Phase 1B | No routing, no posture, no annotation. Complexity only exists when the user has multiple spaces. |
| User-level facts are always global | Identity/structural facts about the user cross all spaces | "User got a new job" applies everywhere, even if learned in the D&D space. |
| Interceptor deferred to Phase 3 | Kernos is reactive in Phase 2 | The LLM's judgment plus soul principles handle behavioral compliance for reactive conversation. The Interceptor matters when agents take unrequested actions (proactive intelligence). |
