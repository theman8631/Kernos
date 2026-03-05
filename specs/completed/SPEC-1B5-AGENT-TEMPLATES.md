# SPEC-1B5: Agent Templates + CLI Fixes

**Status:** READY FOR IMPLEMENTATION
**Depends on:** 1B.1–1B.4 (all complete)
**Objective:** Give the agent a proper identity. Today the system prompt is a hardcoded string with no personality, no memory of the user, and no concept of self. This spec introduces the template structure (what an agent is born from), the hatch process (how the agent becomes personalized for a specific user), and fixes the CLI capabilities display bug.

**What changes for the user:**
Before: "You are Kernos, a personal intelligence assistant. You are in early development."
After: An agent with a warm, consistent personality that knows it's just met this user and naturally discovers who they are through useful conversation — not interrogation.

**What changes architecturally:**
The hardcoded `_build_system_prompt()` in handler.py is replaced by a template-driven prompt assembly that reads the soul, user knowledge, and behavioral contracts from the State Store. The handler's job stays the same (message flow, provisioning, persistence), but the *identity* of the agent it invokes is now a first-class data structure.

**What this is NOT:**
- Not multi-agent (one template: the primary conversational agent)
- Not context spaces (single default context, structure ready for future spaces)
- Not the memory architecture (State Store keeps its current simple implementation)
- Not the hatch interview UX design (the template enables warm bootstrapping; the conversational flow is refined through testing)

-----

## Component 1: Soul Data Model

**New file:** `kernos/kernel/soul.py`

The Soul is the agent's core identity for a specific user. Set at hatch, refined slowly through explicit signals. Persists in the State Store. Consistent across all future context spaces.

```python
from dataclasses import dataclass, field


@dataclass
class Soul:
    """The agent's identity for a specific user.

    Set at hatch (first meeting). Refined slowly through explicit user signals.
    Consistent across all context spaces (Phase 2+). The soul governs HOW the
    agent communicates — values, personality, relationship knowledge. It does NOT
    govern WHAT the agent does (that's behavioral contracts).
    """

    tenant_id: str

    # Identity
    agent_name: str = ""          # May be empty initially; can emerge from conversation
    personality_notes: str = ""   # Free-text personality profile, updated over time

    # User relationship
    user_name: str = ""           # What to call the user
    user_context: str = ""        # Free-text: what the agent knows about the user
    communication_style: str = "" # "direct", "warm", "formal", etc. — inferred or stated
    
    # Lifecycle
    hatched: bool = False         # True after first bootstrap conversation
    hatched_at: str = ""          # ISO timestamp of hatch completion
    interaction_count: int = 0    # Total interactions since hatch
    bootstrap_graduated: bool = False  # True after bootstrap consolidation completes
    bootstrap_graduated_at: str = ""   # ISO timestamp of graduation

    # Workspace scoping (reserved for Phase 2)
    # When implemented: soul belongs to a workspace, not a tenant.
    # Multiple tenants (e.g., household members, plumber + clients) share
    # the same soul/personality but have individual behavioral contracts.
    # workspace_id: str = ""

    # Reserved for Phase 2
    # context_space_postures: dict[str, str] = field(default_factory=dict)
```

**Why free-text fields, not structured enums:** Personality is too fluid for rigid categories. "Warm but direct, uses occasional dry humor, avoids emojis" can't be encoded as enum values. The kernel writes natural language descriptions that get injected into the system prompt. This is how Kit's SOUL.md works — prose, not configuration.

**Why not separate files like OpenClaw:** Kit uses SOUL.md, IDENTITY.md, USER.md as separate markdown files because OpenClaw's substrate is a file workspace. KERNOS's substrate is the State Store. Same conceptual layers, different storage. The Soul dataclass holds what Kit splits across three files: identity (agent_name, personality_notes), user relationship (user_name, user_context, communication_style), and lifecycle metadata.

-----

## Component 2: Agent Template

**New file:** `kernos/kernel/template.py`

The template is the seed — what the agent is before it meets any user. Universal operating principles, default personality traits, and the hatch protocol. One template exists for now: the primary conversational agent.

```python
from dataclasses import dataclass, field


@dataclass
class AgentTemplate:
    """A seed from which an agent is born.

    Contains universal operating principles (shared by all agents in KERNOS),
    default personality (overridden during hatch), and the bootstrap prompt
    (used for the first conversation with a new user).
    """

    name: str                       # "conversational" — the template type
    version: str                    # "0.1" — tracks template evolution

    # The operating principles — KERNOS-universal, not user-specific.
    # These are the agent's bedrock values: intent over instruction,
    # conservative on high-stakes actions, honest about limits, direct.
    # Equivalent to the universal parts of Kit's SOUL.md.
    operating_principles: str

    # Default personality before hatch personalizes it.
    # Warm, curious, slightly informal. Gets replaced by the Soul
    # after hatch, but provides the agent's voice for the first conversation.
    default_personality: str

    # The bootstrap prompt — injected into the system prompt for unhatched
    # tenants. Guides the first conversation: discover who the user is,
    # what they need, be immediately useful, let identity form through action.
    # Equivalent to Kit's BOOTSTRAP.md. Preserved in the Event Stream
    # (never deleted) but not injected after hatch completes.
    bootstrap_prompt: str

    # Capability categories this template expects to work with.
    # Not specific tools — categories like "calendar", "email", "search".
    # Used during hatch to suggest connections.
    expected_capabilities: list[str] = field(default_factory=list)
```

**The primary conversational agent template:**

The `operating_principles`, `default_personality`, and `bootstrap_prompt` strings are the most important text in the system. They're the equivalent of Kit's SOUL.md + BOOTSTRAP.md. Getting them right is iterative — the spec provides the structure and initial content, refinement happens through live testing.

```python
PRIMARY_TEMPLATE = AgentTemplate(
    name="conversational",
    version="0.1",
    operating_principles="""\
You serve one person. Everything you do is in service of understanding their life \
and making it easier. You earn trust through thousands of correct small actions.

INTENT OVER INSTRUCTION. Every request points at an intention. Fulfill the intention, \
not just the literal words. If the words and intention diverge, follow the intention.

CONSERVATIVE BY DEFAULT. Before any action with significant impact — money, access, \
reputation, external communication, irreversibility — verify first. When uncertain \
about stakes, ask. When certain the stakes are low, act.

HONEST ABOUT LIMITS. Never pretend to know something you don't. Never claim a capability \
you don't have. Never fabricate information. Say what you can do, what you can't yet, \
and what you're working on. Honesty builds trust faster than performance.

WHEN YOU'RE WRONG, OWN IT. Don't over-apologize. Don't collapse into self-criticism. \
Acknowledge the mistake, fix what you can, learn from it, move on. The relationship \
survives mistakes. It doesn't survive dishonesty about them.

DIRECT, NOT PERFORMATIVE. Don't perform helpfulness — just help. Don't pad responses \
with unnecessary qualifiers. Say what you mean. If the user wants more detail, they'll ask.

MEMORY IS YOUR RESPONSIBILITY. When something meaningful happens — a preference stated, \
a decision made, a fact shared — hold onto it. Don't wait to be told to remember. \
The more you know about this person, the less they have to explain, and the better \
you serve what they actually need.\
""",
    default_personality="""\
Warm and genuine. Curious about the person you're talking to without being nosy. \
Slightly informal — you're a partner, not a servant. You have a real presence: \
you can be playful when the moment calls for it, serious when it matters, and \
comfortable with silence. You don't fill space with unnecessary words. \
You treat the person's time as valuable.\
""",
    bootstrap_prompt="""\
This is your first conversation with this person. You don't know them yet — \
and that's okay. Be genuinely curious about who they are and what they need, \
but don't interrogate. Let the conversation happen naturally.

Your first priority is to be useful immediately. If they tell you what they need \
help with, help them with it. Through helping, you'll learn who they are — their \
communication style, what matters to them, how they think.

If they tell you their name, remember it. If they share what they do for work, \
what they're struggling with, what excites them — hold onto all of it. \
Every detail is the foundation of a relationship that will grow over months.

Offer to connect capabilities when it feels natural — "I can connect to your calendar \
if that would help" — but don't push. Let the conversation lead.

You are becoming someone specific to this person. Who you become depends on who they \
are and what they need. That's not a limitation — it's the point.\
""",
    expected_capabilities=["calendar", "email", "search"],
)
```

-----

## Component 3: Hatch Process

**Modified file:** `kernos/messages/handler.py`

The hatch process runs once per tenant — the first time they interact. After hatch, the Soul persists and is loaded on every subsequent interaction.

**What happens at hatch:**

1. Tenant sends their first message
2. Handler calls `_ensure_tenant_state()` as today — provisions the tenant
3. Handler detects no Soul exists for this tenant → **hatch mode**
4. Handler creates a default Soul (unhatched) and stores it in State Store
5. System prompt includes the bootstrap prompt from the template
6. Agent responds warmly, begins learning about the user
7. After the response, handler marks the soul as `hatched=True` with timestamp

**Why mark hatched after first response, not after an interview:**

Kit's BOOTSTRAP.md runs a multi-turn interview. For non-technical users texting a phone number, that's friction. KERNOS follows Kit's own advice relayed through OSBuilder: "one question, one useful action, observe." The soul is hatched (born) after the first exchange. It then *refines* continuously through every subsequent interaction.

**Bootstrap persistence and graduation:**

The bootstrap prompt stays in the system prompt for as long as it's useful — potentially weeks for a user who messages infrequently. There is no hard message count cutoff. Instead, the kernel evaluates **soul maturity**: has the soul accumulated enough substance to stand on its own?

Maturity signals (all must be present to trigger graduation):
- `user_name` is populated (the agent knows who it's talking to)
- `user_context` has meaningful content (the agent understands something about their life)
- `communication_style` has been set (the agent has calibrated its tone)
- `interaction_count` exceeds a minimum floor (at least 10 — but this alone is never sufficient)

When maturity is reached, the kernel triggers a **bootstrap consolidation step** before removing the bootstrap from the system prompt. This is a single reasoning call:

> "Review your bootstrap principles and your experience with this user so far. Write any guidance worth preserving into your personality notes and user context. What have you learned about how to serve this person that should be part of your permanent identity?"

The agent's response updates `soul.personality_notes` and `soul.user_context`. The bootstrap's wisdom migrates into the soul — internalized, not discarded. Then the bootstrap prompt stops being injected.

After graduation, the soul carries the relationship forward. The Event Stream preserves the full bootstrap conversation for audit. The bootstrap content remains accessible but is no longer active in the prompt.

For 1B.5 implementation: the maturity check and consolidation step should exist as functions, but if the full consolidation reasoning call adds too much complexity, a simpler fallback is acceptable — just stop injecting bootstrap when maturity signals are met, without the consolidation call. The consolidation call is the ideal; the maturity gate is the minimum.

**State Store additions:**

```python
# In StateStore interface — add to state.py:

@abstractmethod
async def get_soul(self, tenant_id: str) -> Soul | None: ...

@abstractmethod
async def save_soul(self, soul: Soul) -> None: ...
```

**Implementation in JsonStateStore** follows the same pattern as `get_tenant_profile` / `save_tenant_profile` — a JSON file at `{data_dir}/state/{tenant_id}/soul.json`.

-----

## Component 4: Template-Driven System Prompt

**Modified file:** `kernos/messages/handler.py`

Replace the hardcoded `_build_system_prompt()` with a template-driven version that assembles identity from the Soul, contracts from the State Store, and capabilities from the registry.

**Current prompt structure (what gets replaced):**
```
"You are Kernos, a personal intelligence assistant. You are in early development..."
+ platform context
+ auth context
+ capability prompt
```

**New prompt structure:**

```python
def _build_system_prompt(
    message: NormalizedMessage,
    capability_prompt: str,
    soul: Soul,
    template: AgentTemplate,
    contract_rules: list[ContractRule],
) -> str:
    """Build system prompt from template, soul, and live state.

    Layers (in order of injection):
    1. Operating principles — universal KERNOS values
    2. Soul / personality — who the agent is for this user
    3. User knowledge — what the agent knows about this person
    4. Platform context — communication channel constraints
    5. Auth context — sender trust level
    6. Behavioral contracts — what the agent must/must-not do
    7. Capabilities — what tools are available
    8. Bootstrap prompt — ONLY if soul has not graduated (bootstrap_graduated == False)
    """
```

The exact prompt text is the most important content in the system and will be refined through testing. The spec defines the *structure* — which layers exist and their injection order. The initial content comes from `PRIMARY_TEMPLATE` (Component 2) and the Soul (Component 3).

**Contract injection format:**

```
BEHAVIORAL CONTRACTS — follow these strictly:
MUST: Always confirm before any action that costs money
MUST: Always confirm before sending communications on the owner's behalf
MUST NOT: Never send messages to external contacts without owner approval
MUST NOT: Never delete or archive data without owner awareness
PREFERENCE: Keep responses concise unless detail is requested
ESCALATION: Escalate to owner when request is ambiguous and stakes are non-trivial
```

Contract rules are loaded from State Store and formatted into natural language the agent can follow. This replaces embedding rules implicitly in the system prompt with explicit, auditable contract injection.

**Reserved for Phase 2:** The `ContractRule` dataclass in `state.py` should gain an optional `context_space: str | None = None` field. Rules with `context_space=None` apply globally. Context-scoped rules (e.g., "must confirm scheduling in legal context but not in personal context") override within their space. For 1B.5, all rules are global — the field exists but is always None.

**Interaction counter:**

After each successful response, increment `soul.interaction_count` and save. This is the lightest possible refinement signal — the soul knows how many conversations it's had with this person. Used to phase out the bootstrap prompt and (in future) to gate progressive autonomy.

-----

## Component 5: CLI Fixes

**Modified file:** `kernos/cli.py`

### Fix 1: Capabilities display — read from persisted state, not static catalog

**Problem:** CLI reads from `KNOWN_CAPABILITIES` (static catalog) and infers status from env vars. This produced a lie: Gmail showed as `[CONFIGURED]` because the Google OAuth credentials path was set, even though no Gmail MCP server is registered.

**Fix:** CLI reads the persisted registry state from the State Store (tenant profile's `capabilities` field), which reflects actual runtime status. If no tenant-specific state is available (e.g., system-level view), CLI checks whether an MCP server is actually registered for the capability, not just whether credentials exist.

**Vocabulary fix:** Remove `CONFIGURED` as a display label. Use the real vocabulary: `CONNECTED`, `AVAILABLE`, `DISCOVERABLE`, `ERROR`. These are the CapabilityStatus enum values — the CLI should not invent its own terms.

```python
def cmd_capabilities(args) -> None:
    """Display capability registry from persisted state.

    Reads from data directory, same source as runtime.
    Falls back to static catalog with honest status only if no state available.
    """
    # Read from persisted registry state or check actual server registrations
    # NEVER infer status from env vars alone
    # Use CapabilityStatus enum values only — no invented labels
```

### Fix 2: New CLI command — `kernos-cli soul <tenant_id>`

Display the hatched soul for a tenant: agent name, personality notes, user name, user context, communication style, hatched status, interaction count.

```
$ ./kernos-cli soul discord_364303223047323649
────────────────────────────────────────────────────────────
  Soul for discord_364303223047323649
────────────────────────────────────────────────────────────
  Hatched:     2026-03-04T07:12:29+00:00
  Interactions: 6
  User:        (not yet known)
  Agent name:  (default)
  Style:       (not yet determined)
  Personality: Warm and genuine. Curious about the person...
```

### Fix 3: New CLI command — `kernos-cli contracts <tenant_id>`

Display behavioral contract rules for a tenant, grouped by type.

```
$ ./kernos-cli contracts discord_364303223047323649
────────────────────────────────────────────────────────────
  Contracts for discord_364303223047323649
────────────────────────────────────────────────────────────
  MUST:
    - Always confirm before any action that costs money [default]
    - Always confirm before sending communications on behalf [default]
  MUST NOT:
    - Never send messages to external contacts without approval [default]
    - Never delete or archive data without owner awareness [default]
  PREFERENCE:
    - Keep responses concise unless detail is requested [default]
  ESCALATION:
    - Escalate when request is ambiguous and stakes are non-trivial [default]
```

-----

## Implementation Order

1. **Soul data model** (`soul.py`) — the dataclass, nothing else
2. **Template data model** (`template.py`) — the dataclass + `PRIMARY_TEMPLATE` content
3. **State Store additions** — `get_soul`, `save_soul` in interface + JsonStateStore
4. **Hatch process** — handler detects unhatched tenant, creates soul, marks hatched
5. **System prompt refactor** — replace `_build_system_prompt()` with template-driven assembly
6. **CLI capabilities fix** — read from persisted state, use real vocabulary
7. **CLI soul command** — display hatched soul for a tenant
8. **CLI contracts command** — display behavioral contracts for a tenant

Steps 1–5 are the core deliverable. Steps 6–8 are the CLI fixes and inspection tools.

-----

## What Claude Code MUST NOT Change

- Handler message flow (receive → provision → history → reason → persist → respond)
- Adapter isolation (handler knows nothing about Discord/SMS)
- Event emission patterns (message.received, message.sent, etc.)
- Task Engine integration (all work enters through TaskEngine.execute)
- Reasoning Service interface (ReasoningRequest structure)
- Capability Registry runtime behavior in app.py (the correct code — only CLI is broken)
- Existing test coverage

-----

## Acceptance Criteria

1. **New tenant gets bootstrap prompt.** First message from an unknown tenant produces a warm, personalized response that begins learning about the user — not the current generic "You are Kernos" identity.

2. **Returning tenant gets their soul.** Second and subsequent messages load the persisted soul. The agent knows it has talked to this person before (even if it doesn't remember specifics yet — memory architecture is a separate problem).

3. **Contracts are injected and visible.** System prompt includes behavioral contract rules from State Store. `kernos-cli contracts` displays them accurately.

4. **CLI capabilities is honest.** Gmail does NOT show as CONNECTED unless an MCP server is registered and returns tools. Uses real CapabilityStatus vocabulary. No invented labels.

5. **Soul persists across restart.** Stop the system, restart, send a message. The soul loads from the State Store. No re-hatch.

6. **Zero-cost path holds.** Simple messages add no perceptible latency. The template-driven prompt assembly is one State Store read (soul) + one State Store read (contracts) — both are local JSON file reads, sub-millisecond.

7. **Event provenance.** Soul creation emits an event (`agent.hatched`). Soul updates emit events. Formation is auditable via the Event Stream — the bootstrap conversation is never deleted.

-----

## Live Verification

### Prerequisites
- KERNOS running on Discord (existing setup)
- Access to data directory for State Store inspection
- CLI built and accessible

### Test Table

| Step | Action | Expected |
|---|---|---|
| 0 | Agent awareness | Agent identifies itself with personality, knows its platform, lists real capabilities |
| 1 | Delete existing state for your tenant (or use a fresh Discord account) | Clean slate — no soul.json exists |
| 2 | Send first message: "Hey there" | Agent responds warmly, with curiosity about the user — NOT "You are Kernos, a personal intelligence assistant" |
| 3 | Run `kernos-cli soul <tenant_id>` | Shows hatched soul with `hatched: true`, `interaction_count: 1`, `bootstrap_graduated: false` |
| 4 | Share your name in conversation | Agent uses your name naturally in subsequent responses |
| 5 | Run `kernos-cli soul <tenant_id>` again | Shows updated `user_name` field |
| 6 | Restart the system, send another message | Agent personality is consistent — soul loaded from State Store, no re-bootstrap |
| 7 | Run `kernos-cli capabilities` | Gmail shows AVAILABLE (not CONFIGURED/CONNECTED). Calendar shows CONNECTED. Web Search shows AVAILABLE. |
| 8 | Run `kernos-cli contracts <tenant_id>` | Shows all 7 default contract rules, grouped by type |
| 9 | Run `kernos-cli events <tenant_id> --limit 5` | Shows `agent.hatched` event from step 2 |

### Troubleshooting
- If step 2 still shows generic prompt: check that `_build_system_prompt` is reading from template, not hardcoded string
- If step 5 doesn't show user_name: the handler isn't extracting user-stated information from responses yet — that's memory projector work (Phase 2). For 1B.5, user_name is populated if the user explicitly states it AND the handler has extraction logic. If extraction is too complex for this spec, document it as a known gap.
- If step 7 shows Gmail as CONNECTED: the CLI fix didn't land — still reading from static catalog with env var inference

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| One agent, not many | Single primary agent per user with consistent soul | User experiences one entity that knows them. Context spaces (Phase 2) add domain-specific posture, not separate agents. |
| Soul as prose, not config | Free-text personality fields | Personality can't be captured by enums. Natural language in → natural language out. |
| Hatch on first exchange | Mark hatched after first response, not after interview | Non-technical users texting a phone number shouldn't face onboarding friction. Identity forms through action. |
| Bootstrap graduates, not disappears | Maturity-based consolidation, not hard message count | Bootstrap stays useful for as long as the soul is thin. Before removal, agent migrates its wisdom into the soul. Formation is preserved in Event Stream. |
| Soul scoped for future workspace sharing | `workspace_id` reserved in data model | Household members, plumber + clients, and other shared-agent scenarios need the soul to belong to a workspace, not a tenant. Contracts remain per-tenant. Not built now, not blocked later. |
| Contracts explicit in prompt | Inject contract rules as formatted text | Auditable, inspectable, consistent. The agent reads its rules, not implied behavioral hints. |
| CLI reads runtime state | Capabilities from persisted registry, not static catalog | System never claims capabilities it can't deliver. |
| Demo mode works by default | Each sender gets separate tenant_id via `derive_tenant_id()` | Different phone numbers or Discord accounts are separate tenants with separate hatch processes. No special demo configuration needed. |
