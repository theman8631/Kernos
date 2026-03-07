# SPEC-2.0: Schema Foundation Sprint

**Status:** READY FOR IMPLEMENTATION
**Depends on:** Phase 1B complete (all 7 deliverables verified, 369 tests passing)
**Objective:** Plant all Phase 2 data model fields before building any Phase 2 features. Schema changes now, behavior later. Every new field has a safe default that makes existing data valid under the new schema. No migration scripts. No new features. Just foundations.

**Why this exists as a separate spec:**

Both Phase 2 design documents (Memory Architecture + Covenant Model) add fields to shared data structures. If Phase 2A and 2B specs are written independently, they'll either duplicate schema changes or produce incompatible writers. This sprint reconciles the schema once, so every subsequent spec builds on a stable foundation.

**What changes for the user:** Nothing. The agent behaves identically after this sprint. The data on disk gains new fields with defaults. Tests gain coverage for the new fields.

**What changes architecturally:** KnowledgeEntry evolves from 14 fields to ~25. ContractRule evolves into CovenantRule with graduation state. CapabilityInfo gains tool effect declarations. The Event Stream gains new event types. The Tier 2 extraction prompt gains lifecycle archetype classification, foresight signal extraction, and salience estimation. `complete_simple()` gains structured output support.

-----

## Component 1: KnowledgeEntry Evolution

**Modified file:** `kernos/kernel/state.py`

Every new field has a default. Existing `knowledge.json` files deserialize without changes.

```python
@dataclass
class KnowledgeEntry:
    """A piece of knowledge about the user or their world."""

    # --- Identity (unchanged) ---
    id: str                          # "know_{uuid8}"
    tenant_id: str
    content_hash: str                # SHA256[:16] for dedup

    # --- Content (refined) ---
    category: str                    # "entity", "fact", "preference", "pattern"
    subject: str                     # What this is about
    content: str                     # The knowledge text
    confidence: str                  # "stated", "inferred", "observed"
    lifecycle_archetype: str = "structural"  # NEW — "identity" | "structural" | "habitual" | "contextual" | "ephemeral"

    # --- Temporal (extended — bitemporal from Graphiti) ---
    created_at: str = ""             # When the kernel learned this (transaction time)
    expired_at: str = ""             # NEW — When the kernel invalidated this (transaction time)
    valid_at: str = ""               # NEW — When this became true in reality (valid time)
    invalid_at: str = ""             # NEW — When this stopped being true (valid time)
    last_referenced: str = ""        # When last used in context assembly

    # --- Strength (new — dual-strength from Bjork/FSRS) ---
    storage_strength: float = 1.0    # NEW — How well-established. Monotonically increasing.
    last_reinforced_at: str = ""     # NEW — When user last mentioned/confirmed. NOT last_referenced.
    reinforcement_count: int = 1     # NEW — How many times confirmed across conversations

    # --- Foresight (new — inspired by EverMemOS) ---
    foresight_signal: str = ""       # NEW — Forward-looking implication, if any
    foresight_expires: str = ""      # NEW — When the foresight signal becomes irrelevant

    # --- Provenance (extended) ---
    source_event_id: str = ""        # Which event produced this
    source_description: str = ""     # Human-readable provenance
    supersedes: str = ""             # ID of entry this replaces
    entity_node_id: str = ""         # NEW — Link to EntityNode (populated by entity resolution)

    # --- Classification (refined) ---
    tags: list[str] = field(default_factory=list)
    context_space: str = ""          # NEW — Reserved for context spaces (empty = global)
    salience: float = 0.5            # NEW — Initial importance weight (0.0-1.0)

    # --- Status ---
    active: bool = True              # False = shadow archived

    # --- REMOVED: durability ---
    # Subsumed by lifecycle_archetype. Ephemeral archetype = session-scoped.
    # Contextual archetype with foresight_expires = time-bounded.
    # Migration: existing "permanent" → "structural", "session" → "ephemeral",
    # "expires_at:*" → "contextual" with foresight_expires set.
```

**Migration for existing data:**

The `durability` field is being retired. Claude Code should handle this gracefully:

1. If a loaded JSON record has `durability` but no `lifecycle_archetype`, map it:
   - `"permanent"` → `lifecycle_archetype = "structural"`
   - `"session"` → `lifecycle_archetype = "ephemeral"`
   - `"expires_at:*"` → `lifecycle_archetype = "contextual"`, `foresight_expires` = the ISO date
2. If a loaded record has neither field, default to `lifecycle_archetype = "structural"`
3. The `durability` field should be accepted on read (backwards compat) but not written on new entries
4. New entries always use `lifecycle_archetype`, never `durability`

**Retrieval strength is NOT stored — it's computed at read time:**

```python
import math

ARCHETYPE_STABILITY = {
    "identity": 730,      # ~2 years
    "structural": 120,    # ~4 months
    "habitual": 45,       # ~6 weeks
    "contextual": 14,     # ~2 weeks
    "ephemeral": 1,       # ~1 day
}

# FSRS-6 parameter (default — will be tunable from usage data in Phase 3)
W20 = 0.5

def compute_retrieval_strength(entry: KnowledgeEntry, now_iso: str) -> float:
    """Compute current retrieval strength using FSRS-6 power-law decay.

    Called at read time, not stored. Keeps write path clean.
    Returns 0.0-1.0 where 1.0 = fully accessible, 0.0 = effectively forgotten.
    """
    if not entry.last_reinforced_at:
        return 1.0  # New entry, no decay yet

    # Parse timestamps to compute days_since
    from datetime import datetime, timezone
    last = datetime.fromisoformat(entry.last_reinforced_at)
    now = datetime.fromisoformat(now_iso)
    days_since = max((now - last).total_seconds() / 86400, 0)

    if days_since == 0:
        return 1.0

    base_stability = ARCHETYPE_STABILITY.get(entry.lifecycle_archetype, 120)
    effective_stability = base_stability * (1 + 0.1 * math.log1p(entry.storage_strength))

    factor = 0.9 ** (-1 / W20) - 1
    return (1 + factor * days_since / effective_stability) ** (-W20)
```

This function is a utility — it ships in this sprint but is not called by any production code path yet. Phase 2B context assembly will use it for ranking.

-----

## Component 2: ContractRule → CovenantRule Evolution

**Modified file:** `kernos/kernel/state.py`

The class is renamed from `ContractRule` to `CovenantRule`. All existing fields preserved. New fields are additive with defaults.

```python
@dataclass
class CovenantRule:
    """A behavioral rule in the Covenant — the living contract between agent and user."""

    # --- Preserved from ContractRule (backwards compatible) ---
    id: str                          # "rule_{uuid8}"
    tenant_id: str
    capability: str                  # "calendar", "email", "general"
    rule_type: str                   # "must", "must_not", "preference", "escalation"
    description: str                 # Human-readable — what the agent reads and user sees
    active: bool
    source: str                      # "default", "user_stated", "evolved"
    source_event_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    context_space: str | None = None # None = global

    # --- New: Covenant layer ---
    layer: str = "principle"         # "principle" | "practice"

    # --- New: Action class targeting ---
    action_class: str = ""           # "email.delete.spam", "calendar.schedule.business_hours"
    trigger_tool: str = ""           # MCP tool name, e.g. "send_email". Empty = all tools in capability.

    # --- New: Enforcement ---
    enforcement_tier: str = "confirm"  # "silent" | "notify" | "confirm" | "block"
    fallback_action: str = "ask_user"  # "ask_user" | "stage_as_draft" | "log_and_proceed" | "block_with_explanation"
    escalation_message: str = ""       # "Sending to {recipient} — confirm?"

    # --- New: Graduation state (meaningful for Practices only) ---
    graduation_positive_signals: int = 0
    graduation_last_rejection: str = ""     # ISO timestamp, empty = never rejected
    graduation_eligible: bool = False
    graduation_threshold: int = 25          # Parameterized per rule
    graduation_tier_locked_until: str = ""  # Rate limit: can't graduate before this timestamp

    # --- New: Versioning ---
    supersedes: str = ""             # ID of the rule this replaced
    version: int = 1

    # --- Reserved for future phases ---
    agent_id: str = ""               # Phase 3+: agent-specific scoping
    precondition: str = ""           # Phase 3+: dependency graph query
    workspace_id: str = ""           # Phase 3+: workspace-level inheritance
```

**Rename handling:**

1. Rename the class from `ContractRule` to `CovenantRule` in `state.py`
2. Update ALL imports and references across the codebase — handler.py, state_json.py, cli.py, tests
3. The `default_contract_rules()` function becomes `default_covenant_rules()` and returns `CovenantRule` instances
4. Existing `contracts.json` files on disk load correctly — missing new fields get defaults
5. Map existing `rule_type` to `enforcement_tier` for display:
   - `must_not` → `"confirm"`
   - `must` → `"confirm"`
   - `preference` → `"silent"`
   - `escalation` → `"confirm"`

**The `ContractRule` name should be aliased for backwards compatibility in imports:**

```python
# At the bottom of state.py for transition period
ContractRule = CovenantRule
```

-----

## Component 3: EntityNode and IdentityEdge Models

**New file:** `kernos/kernel/entities.py`

Planted now. Not populated until Phase 2A (Entity Resolution).

```python
from dataclasses import dataclass, field


@dataclass
class EntityNode:
    """A distinct entity in the user's world — person, place, organization."""

    id: str                          # "ent_{uuid8}"
    tenant_id: str
    canonical_name: str              # Best/most complete name known
    aliases: list[str] = field(default_factory=list)  # All observed surface forms
    entity_type: str = ""            # "person" | "organization" | "place" | "event" | "other"
    summary: str = ""                # LLM-generated entity summary (updated periodically)
    first_seen: str = ""
    last_seen: str = ""
    conversation_ids: list[str] = field(default_factory=list)
    knowledge_entry_ids: list[str] = field(default_factory=list)  # Back-links
    embedding: list[float] = field(default_factory=list)  # Vector representation
    is_canonical: bool = True        # True if this is the cluster representative
    active: bool = True


@dataclass
class IdentityEdge:
    """Soft identity link between two EntityNodes."""

    source_id: str                   # EntityNode ID
    target_id: str                   # EntityNode ID
    edge_type: str                   # "SAME_AS" | "MAYBE_SAME_AS" | "NOT_SAME_AS"
    confidence: float = 0.0          # 0.0-1.0
    evidence_signals: list[str] = field(default_factory=list)
    created_at: str = ""
    superseded_at: str = ""          # For non-destructive updates


@dataclass
class CausalEdge:
    """Lightweight causal link between KnowledgeEntries. Planted now, populated Phase 3."""

    source_id: str                   # KnowledgeEntry that is the cause
    target_id: str                   # KnowledgeEntry that is the effect
    relationship: str = ""           # "caused_by" | "enables" | "depends_on" | "co_temporal"
    confidence: float = 0.0
    created_at: str = ""
    superseded_at: str = ""
```

**State Store interface additions** (planted, not implemented beyond basic CRUD):

```python
# Add to StateStore ABC in state.py:

# Entity Resolution (Phase 2A will implement real logic)
@abstractmethod
async def save_entity_node(self, node: EntityNode) -> None: ...

@abstractmethod
async def get_entity_node(self, tenant_id: str, entity_id: str) -> EntityNode | None: ...

@abstractmethod
async def query_entity_nodes(
    self, tenant_id: str, name: str | None = None, entity_type: str | None = None
) -> list[EntityNode]: ...

@abstractmethod
async def save_identity_edge(self, edge: IdentityEdge) -> None: ...

@abstractmethod
async def query_identity_edges(
    self, tenant_id: str, entity_id: str
) -> list[IdentityEdge]: ...
```

**JsonStateStore implementation:** Same pattern as knowledge/contracts — JSON files at `{data_dir}/{tenant_id}/state/entities.json` and `{data_dir}/{tenant_id}/state/identity_edges.json`. Basic CRUD only.

-----

## Component 4: CapabilityInfo — Tool Effect Declarations

**Modified file:** `kernos/capability/registry.py`

```python
@dataclass
class CapabilityInfo:
    """A capability the system knows about, regardless of connection status."""

    # --- Existing fields (unchanged) ---
    name: str
    display_name: str
    description: str
    category: str
    status: CapabilityStatus
    tools: list[str] = field(default_factory=list)
    setup_hint: str = ""
    setup_requires: list[str] = field(default_factory=list)
    server_name: str = ""
    error_message: str = ""

    # --- New: Tool effect declarations ---
    tool_effects: dict[str, str] = field(default_factory=dict)
    # Maps tool_name → effect level: "read" | "soft_write" | "hard_write" | "unknown"
    # Example: {"list_events": "read", "create_event": "soft_write", "delete_event": "hard_write"}
    # Tools not in this dict default to "unknown" (treated as hard_write)
```

**Populate for known capabilities in `known.py`:**

```python
# Google Calendar tool effects
tool_effects={
    "get-current-time": "read",
    "list-events": "read",
    "search-events": "read",
    "get-event": "read",
    "create-event": "soft_write",
    "update-event": "soft_write",
    "delete-event": "hard_write",
    "list-calendars": "read",
    "get-calendar": "read",
    "find-free-time": "read",
    # ... map all 13 discovered tools
}
```

For Gmail (when connected): read operations → "read", draft operations → "soft_write", send operations → "hard_write", delete operations → "hard_write".

Unmapped tools default to "unknown" → treated as "hard_write" by the Dispatch Interceptor.

-----

## Component 5: Event Types for Phase 2

**Modified file:** `kernos/kernel/event_types.py`

Add all event types both pillars will need. Adding types is free — they only matter when something emits them.

```python
# --- Covenant lifecycle (Pillar B) ---
COVENANT_EVALUATED = "covenant.evaluated"            # Interceptor evaluated a tool call
COVENANT_ACTION_STAGED = "covenant.action.staged"    # Action pending confirmation
COVENANT_ACTION_APPROVED = "covenant.action.approved" # User approved staged action
COVENANT_ACTION_REJECTED = "covenant.action.rejected" # User rejected staged action
COVENANT_ACTION_EXPIRED = "covenant.action.expired"  # Staged action expired
COVENANT_RULE_GRADUATED = "covenant.rule.graduated"  # Practice graduated tiers
COVENANT_RULE_REGRESSED = "covenant.rule.regressed"  # Practice regressed tiers
COVENANT_RULE_CREATED = "covenant.rule.created"      # New rule from NL parser
COVENANT_RULE_UPDATED = "covenant.rule.updated"      # Rule modified

# --- Entity resolution (Pillar A) ---
ENTITY_CREATED = "entity.created"                    # New EntityNode
ENTITY_MERGED = "entity.merged"                      # Identity link promoted to SAME_AS
ENTITY_LINKED = "entity.linked"                      # MAYBE_SAME_AS edge created

# --- Knowledge lifecycle (Pillar A) ---
KNOWLEDGE_REINFORCED = "knowledge.reinforced"        # Fact re-confirmed by user
KNOWLEDGE_INVALIDATED = "knowledge.invalidated"      # Fact contradicted
KNOWLEDGE_DECAYED = "knowledge.decayed"              # R dropped below threshold (for monitoring)
```

-----

## Component 6: Structured Output Support in complete_simple()

**Modified file:** `kernos/kernel/reasoning.py`

Extend `complete_simple()` to support Anthropic's native structured outputs via `output_config`. This eliminates the 10-15% JSON parse failure rate on Haiku-class models.

```python
async def complete_simple(
    self,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 512,
    prefer_cheap: bool = False,
    output_schema: dict | None = None,  # NEW — JSON Schema for structured output
) -> str:
    """Single stateless completion. No tools, no history, no task events.

    When output_schema is provided, uses Anthropic's native structured outputs
    (constrained decoding). Schema compliance is guaranteed by the API — no
    json.loads() retry logic needed. Still must check stop_reason for truncation.
    """
```

**When `output_schema` is provided:**
- Pass `output_config={"format": {"type": "json_schema", "schema": output_schema}}` to the API call
- Check `stop_reason` — if `"max_tokens"`, log warning and return `"{}"`
- Check `stop_reason` — if `"refusal"`, log and return `"{}"`
- Return the raw text (guaranteed valid JSON matching the schema)

**When `output_schema` is None:** Existing behavior unchanged.

**Update the Tier 2 extraction pipeline** (`kernos/kernel/projectors/llm_extractor.py`):
- Define the extraction schema as a Python dict matching the current expected format (entities, facts, preferences, corrections arrays)
- Add a `reasoning` field before the data arrays (60% accuracy improvement per research)
- Pass the schema to `complete_simple(output_schema=EXTRACTION_SCHEMA)`
- Remove the markdown fence stripping code
- Remove the `json.loads()` try/except fallback
- Keep `json.loads()` on the result (still needed to parse the guaranteed-valid JSON string into a dict)
- Add stop_reason handling for truncation

**The extraction schema:**

```python
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief analysis of what knowledge is present"
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "relation": {"type": "string"},
                    "durability": {"type": "string"}
                },
                "required": ["name", "type", "relation", "durability"],
                "additionalProperties": False
            }
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "string"},
                    "lifecycle_archetype": {"type": "string"},
                    "foresight_signal": {"type": "string"},
                    "foresight_expires": {"type": "string"},
                    "salience": {"type": "string"}
                },
                "required": ["subject", "content", "confidence", "lifecycle_archetype"],
                "additionalProperties": False
            }
        },
        "preferences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "string"},
                    "lifecycle_archetype": {"type": "string"}
                },
                "required": ["subject", "content", "confidence", "lifecycle_archetype"],
                "additionalProperties": False
            }
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "old_value": {"type": "string"},
                    "new_value": {"type": "string"}
                },
                "required": ["field", "old_value", "new_value"],
                "additionalProperties": False
            }
        }
    },
    "required": ["reasoning", "entities", "facts", "preferences", "corrections"],
    "additionalProperties": False
}
```

**All fields are required, not optional** — this consumes zero optional-parameter budget and avoids the exponential grammar compilation cost of nullable types.

-----

## Component 7: Tier 2 Extraction Prompt Update

**Modified file:** `kernos/kernel/projectors/llm_extractor.py`

Update the extraction system prompt to request the new fields:

1. **lifecycle_archetype** — add to the prompt: "Classify each fact's lifecycle archetype: identity (name, birthday — rarely changes), structural (employer, city — changes infrequently), habitual (preferences, routines — gradual drift), contextual (current project, upcoming event — changes regularly), ephemeral (current mood, today's plan — expires quickly)."

2. **foresight_signal** + **foresight_expires** — add: "If a fact has a time-bounded forward-looking implication, include it as a foresight signal with an expiration date. Example: 'User is on antibiotics until next Friday' → foresight_signal: 'Avoid recommending alcohol', foresight_expires: 'YYYY-MM-DDT...'."

3. **salience** — add: "Rate the importance of each extracted fact from 0.0 (trivial aside) to 1.0 (central to the user's life or current concerns). Most facts are 0.3-0.5. Facts from the main conversation topic score higher."

4. Use the structured output schema (Component 6) instead of "Return JSON only."

-----

## Component 8: PendingAction Model

**New addition to:** `kernos/kernel/state.py`

Planted now for the Dispatch Interceptor (Phase 2B).

```python
@dataclass
class PendingAction:
    """A tool call staged for user confirmation by the Dispatch Interceptor."""

    id: str                     # "pending_{uuid8}"
    tenant_id: str
    rule_id: str                # Which CovenantRule triggered this
    tool_name: str
    tool_arguments: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)  # Enough state to resume
    created_at: str = ""
    expires_at: str = ""        # Default: 1 hour from creation
    status: str = "pending"     # "pending" | "approved" | "rejected" | "expired"
    conversation_id: str = ""
    batch_id: str = ""          # Groups tool calls from the same reasoning turn
```

State Store interface addition:

```python
@abstractmethod
async def save_pending_action(self, action: PendingAction) -> None: ...

@abstractmethod
async def get_pending_actions(self, tenant_id: str, status: str = "pending") -> list[PendingAction]: ...

@abstractmethod
async def update_pending_action(self, tenant_id: str, action_id: str, updates: dict) -> None: ...
```

-----

## Implementation Order

1. **KnowledgeEntry evolution** — add fields, durability migration logic, retrieval strength utility function
2. **ContractRule → CovenantRule rename + evolution** — rename class, update all imports, add fields, update default_covenant_rules()
3. **EntityNode + IdentityEdge + CausalEdge models** — new file, State Store interface additions, basic JsonStateStore CRUD
4. **PendingAction model** — new dataclass, State Store interface additions, basic CRUD
5. **CapabilityInfo tool effect declarations** — add field, populate for known capabilities
6. **Event types** — add all Phase 2 event types
7. **Structured output support** — extend complete_simple(), update Tier 2 extractor with schema and new prompt
8. **Update CLI** — `kernos-cli knowledge` shows new fields (archetype, R strength, foresight). `kernos-cli contracts` shows CovenantRule fields (layer, enforcement_tier, graduation state).
9. **Update all tests** — new field defaults, rename references, structured output mocks

-----

## What Claude Code MUST NOT Change

- Handler message flow (process() ordering, adapter isolation)
- Template content (operating principles, personality, bootstrap)
- Reasoning Service execute() method (only complete_simple() gains the schema parameter)
- Soul data model (no changes)
- Event Stream emit/query interface (only new types added)

-----

## Acceptance Criteria

1. **Existing data loads.** All existing `knowledge.json`, `contracts.json`, `soul.json` files deserialize correctly under the new schemas. Missing fields get safe defaults.

2. **Durability migration works.** Existing KnowledgeEntries with `durability: "permanent"` load as `lifecycle_archetype: "structural"`. Session → ephemeral. Expires_at → contextual with foresight_expires set.

3. **CovenantRule rename is complete.** `ContractRule` no longer exists except as a backwards-compat alias. All imports, tests, CLI commands, and handler code reference `CovenantRule`.

4. **Structured outputs work.** `complete_simple()` with an `output_schema` parameter returns valid JSON matching the schema. Truncation and refusal are handled gracefully (return `"{}"`).

5. **Tier 2 extraction uses structured outputs.** The markdown fence stripping fallback is removed. The extraction prompt requests lifecycle_archetype, foresight_signal, foresight_expires, and salience. New fields are populated on extracted KnowledgeEntries.

6. **Retrieval strength utility works.** `compute_retrieval_strength()` returns sensible values: 1.0 for fresh entries, decaying values for older ones, faster decay for ephemeral archetypes, slower for identity.

7. **Entity models exist.** EntityNode, IdentityEdge, CausalEdge dataclasses exist. State Store has basic CRUD for entities and edges. No entity resolution logic yet.

8. **PendingAction model exists.** Dataclass and basic State Store CRUD. No Dispatch Interceptor logic yet.

9. **Tool effect declarations exist.** CapabilityInfo has `tool_effects` field. Google Calendar tools are mapped. Unmapped tools report "unknown".

10. **All event types defined.** All Phase 2 event types exist in EventType enum. None are emitted yet.

11. **CLI shows new fields.** `kernos-cli knowledge` displays lifecycle_archetype and computed retrieval strength. `kernos-cli contracts` displays layer, enforcement_tier, graduation state.

12. **All existing tests pass.** 369+ tests still green. New tests cover schema evolution, durability migration, retrieval strength computation, structured output handling.

-----

## Live Verification

**N/A — schema and infrastructure sprint.** No user-facing changes. Verification is: all tests pass, existing data loads correctly, `kernos-cli` displays new fields with default values for existing entries.

**Founder review:** After Claude Code completes, run:
```bash
python -m pytest tests/ -v
./kernos-cli knowledge <tenant_id>
./kernos-cli contracts <tenant_id>
```

Verify: knowledge shows lifecycle_archetype and retrieval strength. Contracts show as CovenantRule with layer and enforcement_tier. Existing data intact.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Durability retired | lifecycle_archetype subsumes it | One classification system, not two. Archetype drives decay, verification, cascades. |
| CovenantRule not ContractRule | Rename to match Covenant Model vocabulary | The Covenant metaphor (collaborative commitment, not adversarial enforcement) carries through the codebase. |
| Retrieval strength computed, not stored | FSRS-6 formula applied at read time | Keeps write path clean. Avoids computing decay for unaccessed facts. |
| Entity models planted but empty | Schema exists, no resolution logic | Phase 2A builds on a stable schema. No premature optimization of resolution algorithms. |
| CausalEdge planted but empty | Schema exists, no inference logic | Phase 3 builds the causal layer. The schema is ready when we are. |
| Structured outputs on complete_simple() | Anthropic native output_config | Eliminates 10-15% JSON parse failures. Zero-retry property critical for high-frequency extraction pipeline. |
| All fields required in extraction schema | No optional/nullable types | Zero optional-parameter budget consumed. Avoids exponential grammar compilation cost. |
| Tool effects on CapabilityInfo | Reversibility is a property of the tool, not the rule | One declaration covers all rules touching a tool. Enables zero-cost-path for read-only tools. |
| Event types planted preemptively | All Phase 2 types defined now | Avoids adding types piecemeal across specs. One reconciled set. |
