# SPEC-1B7: Basic Memory Projectors

**Status:** READY FOR IMPLEMENTATION
**Depends on:** 1B.1–1B.6 (all complete)
**Objective:** Make the soul actually learn from conversations. Today the soul has fields for `user_name`, `user_context`, and `communication_style`, but nothing populates them — they stay empty until someone manually edits the JSON file. This spec adds two-tier knowledge extraction that closes the loop: conversations produce knowledge, knowledge updates the soul, the soul reaches maturity, the bootstrap graduates. The agent becomes someone who knows you.

**Why this matters now, not Phase 2:**

Memory is the moat (Pillar 3). Without extraction, the soul is an empty shell. The bootstrap can never graduate because the maturity signals (`user_name` populated, `user_context` has substance, `communication_style` set, `interaction_count > 10`) depend on data that never arrives. The agent asks "What's your name?" and John answers, but the system forgets by next session. That's a broken promise. Basic memory projectors are the minimum that makes the hatched agent real.

**Architecture principle — "the agent thinks, the kernel remembers":**

The agent's job is conversation. The kernel's job is remembering what matters. These two responsibilities must not be coupled. The agent does NOT extract its own memories — the kernel observes the conversation and does the extraction work separately. Two tiers, two different mechanisms, both kernel-owned.

**What this is NOT:**
- Not semantic search (keyword/indexed lookup in State Store is sufficient for now)
- Not the consolidation daemon (pattern extraction across weeks — that's Phase 2)
- Not memory decay or compaction (Phase 2 memory architecture)
- Not the inline annotation engine (Phase 2 context assembly)

-----

## The Two-Tier Architecture

### Tier 1: Rule-Based Soul Extraction (synchronous, zero cost)

**When:** After the LLM response is assembled, before the response is returned to the user.
**Mechanism:** Python pattern matching against the conversation (user message + assistant response).
**Cost:** Zero — no LLM call, pure string operations.
**Latency:** Sub-millisecond. Invisible.
**What it extracts:** Soul-relevant signals only.

| Signal | Patterns | Soul field |
|---|---|---|
| Name | "I'm X", "my name is X", "call me X", "I go by X", "it's X" | `user_name` |
| Style | "keep it casual", "be direct", "don't sugarcoat", "keep it brief", "I like detail" | `communication_style` |
| Context | "I'm a [occupation]", "I work in/at [field/company]", "I live in [place]", "I'm based in [place]" | `user_context` (append) |
| Agent name | "I'll call you X", "your name is X", "let's call you X" | `agent_name` |
| Corrections | "actually call me X", "it's X not Y", "I moved to X" | Updates the relevant field, superseding previous value |

**Pattern matching approach:** Not fragile regex looking for exact strings. Use a set of prefix patterns and lightweight parsing. "I'm a plumber in Portland" should match even though it's not in the pattern table verbatim — "I'm a [word+] in [word+]" captures occupation + location in one pass.

**Important constraints:**
- Only extract from the USER's messages, not the assistant's responses.
- Only write to soul fields, not KnowledgeEntry records (that's Tier 2's job).
- If a field already has a value and a new value is detected, overwrite — the user's latest statement wins.
- Every soul update emits a `knowledge.extracted` event with source: "rule_engine".

### Tier 2: Async LLM Knowledge Extraction (asynchronous, low cost)

**When:** After the response has been sent to the user. Fires as an async task. User never waits.
**Mechanism:** A separate, lightweight LLM call against the recent conversation tail.
**Cost:** ~$0.003-0.005 per message (short input, structured output, same model for now — cheap model routing is Phase 2).
**Latency:** Zero user-perceived. 500ms-1s actual, but response already sent.
**What it extracts:** Structured knowledge — entities, facts, preferences, corrections.

**The extraction prompt:**

```
You are a knowledge extraction engine. Read the following conversation excerpt 
and extract any meaningful information about the user or their world.

Return ONLY a JSON object with this structure, no other text:
{
  "entities": [{"name": "...", "type": "person|place|org|other", "relation": "..."}],
  "facts": [{"subject": "...", "content": "...", "confidence": "stated|inferred"}],
  "preferences": [{"subject": "...", "content": "...", "confidence": "stated|inferred"}],
  "corrections": [{"field": "...", "old_value": "...", "new_value": "...", "confidence": "stated"}]
}

Rules:
- "stated" = the user explicitly said this. "inferred" = you deduced it from context.
- Only extract what's genuinely new or meaningful. Don't extract greetings or small talk.
- If nothing meaningful was said, return empty arrays.
- Be conservative — when in doubt, don't extract.

Conversation:
{last_n_turns}
```

**Input:** The last 2-4 conversation turns (user + assistant messages). Not the full history — just the recent window. This keeps the extraction call short and cheap.

**Output processing:**
- Parse the JSON response (with error handling — if parsing fails, log and skip, don't crash).
- For each extracted item, create a `KnowledgeEntry` and write to State Store.
- Deduplication: before writing, check if an entry with the same `subject` + `content` already exists for this tenant. If so, skip. This makes extraction idempotent.
- For corrections: find the existing entry being corrected, mark it `active=False`, create a new entry with `supersedes` pointing to the old entry's ID.
- Every write emits a `knowledge.extracted` event with source: "llm_extractor".

**Confidence distinction matters:**
- `stated` = user explicitly said it ("I'm a plumber"). High certainty. Should override inferred knowledge.
- `inferred` = deduced from context ("seems to work with their hands based on scheduling patterns"). Lower certainty. Should NOT override stated knowledge.

When a stated extraction conflicts with an existing inferred entry, the stated entry wins and the inferred entry is marked inactive. When an inferred extraction conflicts with an existing stated entry, the inferred entry is discarded.

-----

## Component 1: KnowledgeEntry Model Update

**Modified file:** `kernos/kernel/state.py`

Add three fields to KnowledgeEntry:

```python
@dataclass
class KnowledgeEntry:
    """A piece of knowledge about the user or their world."""

    id: str                     # "know_{uuid8}"
    tenant_id: str
    category: str               # "entity", "fact", "preference", "pattern"
    subject: str                # "John", "gym membership", "meeting preferences"
    content: str                # The knowledge text
    confidence: str             # "stated", "inferred", "observed"
    source_event_id: str        # Provenance — links to the event that created this
    source_description: str     # Human-readable provenance
    created_at: str
    last_referenced: str
    tags: list[str]
    active: bool = True         # False = archived (never deleted)
    supersedes: str = ""        # ID of the entry this one replaces (provenance chain)
    durability: str = "permanent"  # "permanent" | "session" | "expires_at:<ISO>"
    content_hash: str = ""      # SHA256 of (tenant_id + subject + content) for dedup
```

**Durability semantics:**
- `"permanent"` — indefinite. Who the user is, how they communicate, what they do.
- `"session"` — relevant only to current conversation. Discard at session end.
- `"expires_at:2026-03-06T10:00:00Z"` — time-bound. Appointment times, temporary states.

Phase 2 reads durability during context assembly and decay. For now, the field is populated by Tier 2 extraction but not yet acted on. Adding it now is cheap. Retrofitting after the store is full of expired temporal facts is painful.

**Content hash generation:**
```python
import hashlib

def _content_hash(tenant_id: str, subject: str, content: str) -> str:
    raw = f"{tenant_id}|{subject.lower().strip()}|{content.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

Used for idempotent deduplication. If a hash already exists in the active entries for this tenant, skip writing. Same conversation processed twice produces the same entries, not duplicates.

**Supersedes semantics:**
When a correction arrives ("actually call me JT"), the kernel:
1. Creates a new entry with the corrected value
2. Sets `supersedes` to the old entry's ID
3. Marks the old entry `active = False`

The event stream preserves full history. The State Store reflects current truth.

**New State Store methods needed:**

```python
# Add to StateStore ABC in state.py:

@abstractmethod
async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
    """Write or update a KnowledgeEntry."""
    ...

@abstractmethod
async def get_knowledge_hashes(self, tenant_id: str) -> set[str]:
    """Return set of content_hash values for all active entries for this tenant.
    Used for O(1) deduplication before writing."""
    ...

@abstractmethod
async def get_knowledge_by_hash(self, tenant_id: str, content_hash: str) -> KnowledgeEntry | None:
    """Find an active entry by content_hash. Returns None if not found."""
    ...
```

JsonStateStore implements these against `{data_dir}/{tenant_id}/state/knowledge.json`, same pattern as existing knowledge methods.

-----

## Component 2: Tier 1 — Rule-Based Soul Extractor

**New file:** `kernos/kernel/projectors/rules.py`

```python
"""Tier 1 rule-based extractor.

Synchronous. Zero LLM cost. Writes soul fields only.
Conservative scope: user_name and communication_style.
Anything semantic (user_context, entities, corrections) goes to Tier 2.
"""
```

**When:** After the LLM response is assembled, before the response is returned to the user.
**Mechanism:** Python pattern matching against the user's message only (never the assistant's response).
**Cost:** Zero — no LLM call, pure string operations.
**Latency:** Sub-millisecond. Invisible.
**Scope:** Only `user_name` and `communication_style`. Nothing else.

**Why only two fields:**

`user_name` and `communication_style` have tight, unambiguous patterns where rule-based extraction is reliable. `user_context` is semantic — "I'm a plumber in Portland" and "my buddy's a plumber in Portland" match the same regex but have different subjects. Extracting wrong context into the soul is worse than extracting nothing. Context goes to Tier 2 where the LLM can distinguish subject, negation, and intent.

**Name detection patterns:**
```python
# "I'm {name}", "my name is {name}", "call me {name}",
# "they call me {name}", "everyone calls me {name}", "it's {name}"
# Edge case protection: reject common false positives
# "I'm fine", "I'm ready", "I'm good" → not names
# Approach: match pattern, validate candidate is capitalized and not 
# in the false positive list

_FALSE_POSITIVE_NAMES = {
    "not", "fine", "good", "here", "ready", "back", "sure", "okay",
    "done", "set", "trying", "looking", "going", "working", "just",
    "also", "kind", "sort", "the", "a", "an", "new",
}
```

**Style detection patterns:**
```python
# "keep it casual/chill/informal" → "casual"
# "be direct/straight/blunt with me" → "direct"
# "don't sugarcoat / no need to be formal" → "casual, direct"
# "hate when it's formal" → "casual"
# "keep it professional/formal" → "formal"
```

**Behavior constraints:**
- Only extract from the USER's message, never the assistant's response.
- Only write to soul fields, not KnowledgeEntry records (that's Tier 2's job).
- Only extract if the soul field is currently empty. Tier 1 won't overwrite existing values — corrections look identical to initial statements in regex and require Tier 2's contextual understanding.
- Every soul update emits a `knowledge.extracted` event with source: "tier1_rules".

**Return format:** A dataclass with optional `user_name` and `communication_style` fields. Empty strings mean nothing extracted.

-----

## Component 3: Tier 2 — Async LLM Knowledge Extractor

**New file:** `kernos/kernel/projectors/llm_extractor.py`

**When:** After the response has been sent to the user. Fires as an async task. User never waits.
**Mechanism:** A separate, lightweight LLM call via `ReasoningService.complete_simple()`.
**Cost:** ~$0.003-0.005 per message (short input, structured output, same model for now — cheap model routing is Phase 2).
**Latency:** Zero user-perceived. 500ms-1s actual, but response already sent.
**What it extracts:** Structured knowledge — entities, facts, preferences, corrections — with durability classification.

**The extraction prompt:**

```python
_EXTRACTION_SYSTEM_PROMPT = """You extract knowledge worth remembering from conversations.

WORTH PERSISTING (permanent facts about the person):
- Who they are: occupation, role, location, life situation
- What they care about: goals, problems they're solving, values
- How they operate: work patterns, communication preferences, decision-making style
- Relationships: people they mention by name and their relation to the user
- Stated preferences: things they explicitly like, dislike, or want handled a certain way

NOT WORTH PERSISTING:
- Specific appointment times, dates, or task outcomes (these expire)
- Questions they asked or information you provided
- Greetings, pleasantries, filler
- Things that were true only in the moment ("I'm running late")

CORRECTIONS:
If the user corrects something previously stated ("actually call me JT", "wait, I meant Tuesday"),
emit a correction entry. The kernel will handle marking the old entry inactive.

Return JSON only. No explanation. Schema:

{
  "entities": [
    {"name": "string", "type": "person|place|org", "relation": "string", "durability": "permanent"}
  ],
  "facts": [
    {"subject": "user|entity_name", "content": "string", "confidence": "stated|inferred", "durability": "permanent|session|expires_at:<ISO>"}
  ],
  "preferences": [
    {"subject": "string", "content": "string", "confidence": "stated|inferred", "durability": "permanent"}
  ],
  "corrections": [
    {"field": "string", "old_value": "string", "new_value": "string"}
  ]
}

If nothing is worth persisting, return: {"entities": [], "facts": [], "preferences": [], "corrections": []}"""
```

**Input:** The last 2-4 conversation turns from the existing `ConversationStore.get_recent()` — the handler already loads this as `history`. No new read method needed. If fewer than 2 turns exist, still extract (the first exchange often has the richest identity signal). Cap at 4 turns to keep the call cheap.

**The LLM call uses `complete_simple()` on ReasoningService** — a new method that does a single stateless completion without tools, conversation history, or task events. This is a kernel infrastructure call, not an agent reasoning call.

**New method on ReasoningService:**

```python
async def complete_simple(
    self,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 512,
    prefer_cheap: bool = False,
) -> str:
    """Single stateless completion. No tools, no history, no task events.
    
    Used by kernel infrastructure (extraction, consolidation) not by agents.
    Returns raw text response. When prefer_cheap is True and multiple models
    are configured, routes to the cheapest. With one model configured,
    uses that model regardless of prefer_cheap.
    """
```

**Output processing:**
- Parse the JSON response. Tolerate markdown code fences (`\`\`\`json ... \`\`\``) — strip them before parsing.
- If parsing fails, log the error and return empty result. Never crash, never retry.
- For each extracted item, build a `KnowledgeEntry` with `content_hash` computed.
- Check hash against existing active entries via `state.get_knowledge_hashes(tenant_id)` — O(1) dedup check.
- If hash exists, skip. If not, write via `state.save_knowledge_entry()`.
- For user-subject permanent facts, also append to `soul.user_context` and save the soul.
- For corrections, find the old entry by hash, mark inactive, create new entry with `supersedes`.

**Confidence precedence:**
- `stated` = user explicitly said it. High certainty. Overrides inferred knowledge.
- `inferred` = deduced from context. Lower certainty. Does NOT override stated knowledge.
- When a stated extraction conflicts with an existing inferred entry, stated wins and inferred is marked inactive.
- When an inferred extraction conflicts with an existing stated entry, the inferred entry is discarded.

-----

## Component 4: Projector Coordinator

**New file:** `kernos/kernel/projectors/coordinator.py`

Orchestrates both tiers, writes results to State Store, emits events.

```python
"""Projector coordinator — runs after every response.

Tier 1 runs synchronously (regex, <1ms, zero cost).
Tier 2 fires as an async background task (does not block the response).
"""

async def run_projectors(
    *,
    user_message: str,
    recent_turns: list[dict],  # From existing ConversationStore.get_recent()
    soul: Soul,
    state: StateStore,
    events: EventStream,
    reasoning_service,         # ReasoningService
    tenant_id: str,
) -> None:
    """Entry point called by handler after response is assembled.

    Tier 1 runs immediately (synchronous, <1ms).
    Tier 2 is scheduled as a background asyncio task (non-blocking).
    """
    # --- Tier 1: synchronous, zero cost ---
    # Extracts only user_name and communication_style
    t1_result = tier1_extract(user_message, soul.user_name, soul.communication_style)
    
    soul_updated = False
    if t1_result.user_name and not soul.user_name:
        soul.user_name = t1_result.user_name
        soul_updated = True
    if t1_result.communication_style and not soul.communication_style:
        soul.communication_style = t1_result.communication_style
        soul_updated = True
    
    if soul_updated:
        await state.save_soul(soul)
        await emit_knowledge_event(events, tenant_id, "tier1_rules", {...})

    # --- Tier 2: async, does not block ---
    asyncio.create_task(
        _run_tier2(recent_turns, soul, state, events, reasoning_service, tenant_id)
    )
```

Tier 2 in the background task:
- Calls `reasoning_service.complete_simple()` with the extraction prompt
- Parses JSON output
- Loads existing hashes via `state.get_knowledge_hashes(tenant_id)` — one read, O(1) per-entry dedup
- Writes new entries via `state.save_knowledge_entry()`
- For user-subject permanent facts, appends to `soul.user_context` and saves soul
- For corrections, applies via `_apply_correction()` which handles supersedes chain and soul field updates
- Errors are logged, never raised — user's response was already sent

**New files structure:**
```
kernos/kernel/projectors/__init__.py        # Package init
kernos/kernel/projectors/coordinator.py     # run_projectors() — main entry point
kernos/kernel/projectors/rules.py           # Tier 1 rule-based extractor
kernos/kernel/projectors/llm_extractor.py   # Tier 2 async LLM extractor
```

-----

## Component 5: Name Ask (First Interaction Safety Net)

**Modified file:** `kernos/messages/handler.py`

If the user's first message doesn't include their name (Tier 1 didn't extract one), and the LLM's response doesn't already ask for it, append a natural name question.

```python
def _maybe_append_name_ask(response_text: str, soul: Soul) -> str:
    """On first interaction, if name still unknown, ask naturally.
    
    Only fires once (interaction_count == 1). Only if Tier 1 didn't catch a name.
    Only if the response doesn't already contain a name question.
    """
    if soul.interaction_count != 1 or soul.user_name:
        return response_text
    # Don't double-ask if the LLM already asked
    name_question_signals = ["your name", "call you", "who am i talking", "what should i call"]
    if any(signal in response_text.lower() for signal in name_question_signals):
        return response_text
    return response_text.rstrip() + "\n\nBy the way — what should I call you?"
```

Runs after Tier 1 extraction, before the response is sent. This collapses the most important maturity signal from potentially many messages to one exchange. Feels like politeness, not onboarding.

-----

## Component 6: Handler Integration

**Modified file:** `kernos/messages/handler.py`

Two integration points in the `process()` method. The existing flow is unchanged — projectors are additive:

```python
# ... existing flow: load soul, build prompt, execute task engine ...

response_text = task.result_text

# NEW: Tier 1 runs sync, may update soul fields
await run_projectors(
    user_message=message.content,
    recent_turns=history[-4:],  # Reuse existing history variable, last 4 turns
    soul=soul,
    state=self.state,
    events=self.events,
    reasoning_service=self.reasoning,
    tenant_id=tenant_id,
)
# NOTE: run_projectors returns after Tier 1 completes; Tier 2 fires as background task

# NEW: Append name ask if still needed (checks soul.user_name post-Tier-1)
response_text = _maybe_append_name_ask(response_text, soul)

# Existing: soul update (interaction_count, hatch check, maturity check)
await self._post_response_soul_update(soul)

# Existing: store assistant response, emit message.sent, update conversation summary
# ... rest of existing flow unchanged ...
```

**Important ordering:**
1. Task engine executes (LLM response assembled)
2. `run_projectors()` — Tier 1 runs sync, updates soul fields if found, kicks off Tier 2 async
3. `_maybe_append_name_ask()` — only if Tier 1 didn't catch a name on first interaction
4. `_post_response_soul_update()` — increments interaction_count, checks maturity with freshly-updated soul fields
5. Response stored and sent
6. Tier 2 completes in background (may update soul.user_context, write knowledge entries)

-----

## Component 7: Bootstrap Consolidation

**Modified in:** `kernos/messages/handler.py`

When `_is_soul_mature()` returns True, trigger the consolidation reasoning call. Uses `complete_simple()` — no tools, no task events.

```python
async def _consolidate_bootstrap(self, soul: Soul) -> None:
    """One-time consolidation: bootstrap wisdom → soul personality notes.
    
    Uses complete_simple() — stateless, no tools, no task events.
    """
    from kernos.kernel.template import PRIMARY_TEMPLATE
    
    prompt = (
        "You are reflecting on your first interactions with a user.\n\n"
        f"Bootstrap intent:\n{PRIMARY_TEMPLATE.bootstrap_prompt}\n\n"
        f"What you've learned:\n"
        f"- Name: {soul.user_name or 'unknown'}\n"
        f"- Context: {soul.user_context or 'unknown'}\n"
        f"- Style: {soul.communication_style or 'unknown'}\n"
        f"- Interactions: {soul.interaction_count}\n\n"
        "Write 2-3 sentences of personality notes — how you'll approach "
        "this person, what matters to them, what tone fits. Be specific. "
        "Don't repeat facts already captured above. Write for the agent, "
        "not the user."
    )
    
    try:
        notes = await self.reasoning.complete_simple(
            system_prompt="You are writing internal notes for an AI agent about their relationship with a specific user.",
            user_content=prompt,
            max_tokens=200,
        )
        soul.personality_notes = notes.strip()
    except Exception as exc:
        logger.warning(
            "Bootstrap consolidation failed for %s: %s — graduating without consolidation",
            soul.tenant_id, exc,
        )
    # Graduate regardless — consolidation is ideal, not required
```

**Cost:** ~$0.02, one time only per tenant.

**Integration into `_post_response_soul_update`:**

```python
if not soul.bootstrap_graduated and _is_soul_mature(soul):
    await self._consolidate_bootstrap(soul)
    soul.bootstrap_graduated = True
    soul.bootstrap_graduated_at = now
    # ... existing event emission ...
```

**Graduation is unconditional.** If the consolidation call fails, the soul still graduates — it just won't have enriched personality notes. The alternative (retrying every interaction until consolidation succeeds) risks a failure loop. The soul has enough substance to carry the relationship without the consolidation notes — that's what the maturity check verified.

-----

## Component 8: Event Types

**Modified file:** `kernos/kernel/event_types.py`

Add:
```python
KNOWLEDGE_EXTRACTED = "knowledge.extracted"
```

-----

## Component 9: CLI — Knowledge Inspection

**Modified file:** `kernos/cli.py`

New command: `kernos-cli knowledge <tenant_id>`

```
$ ./kernos-cli knowledge discord_364303223047323649
────────────────────────────────────────────────────────────
  Knowledge for discord_364303223047323649  (5 entries)
────────────────────────────────────────────────────────────
  [stated] fact: "User is a plumber" (2026-03-05)
  [stated] fact: "User is based in Portland" (2026-03-05)
  [stated] preference: "Prefers casual, informal tone" (2026-03-05)
  [stated] entity: "Mrs. Henderson — client appointment" (2026-03-05)
  [inferred] fact: "Struggling with scheduling" (2026-03-05)
```

Displays active entries only by default. `--all` flag shows inactive (archived) entries too with `[archived]` label.

-----

## Implementation Order

1. **KnowledgeEntry model update** — add `supersedes`, `durability`, `content_hash` fields
2. **State Store additions** — `save_knowledge_entry`, `get_knowledge_hashes`, `get_knowledge_by_hash`
3. **`complete_simple()` on ReasoningService** — stateless completion method for kernel infrastructure
4. **Event type** — add `knowledge.extracted`
5. **Tier 1 SoulExtractor** — `kernos/kernel/projectors/rules.py` — pattern matching for name and style only
6. **Tier 2 KnowledgeExtractor** — `kernos/kernel/projectors/llm_extractor.py` — async LLM extraction with durability
7. **Projector coordinator** — `kernos/kernel/projectors/coordinator.py` — orchestrates both tiers, dedup, corrections
8. **Name ask** — `_maybe_append_name_ask()` in handler
9. **Handler integration** — wire `run_projectors()` and name ask into `process()` flow
10. **Bootstrap consolidation** — `_consolidate_bootstrap()` using `complete_simple()`, integrate into maturity gate
11. **CLI knowledge command** — display extracted knowledge per tenant
12. **Tests** — extraction patterns, dedup (content_hash), confidence precedence, corrections (supersedes chain), consolidation, name ask, idempotency

-----

## What Claude Code MUST NOT Change

- Handler message flow structure (receive → provision → reason → respond) — projectors are additive to this flow
- Soul data model fields (only KnowledgeEntry gets new fields)
- Template content (operating principles, personality, bootstrap — all just refined in 1B.5)
- Event stream / State Store existing interfaces (add new methods, don't change existing ones)
- Reasoning Service existing `execute()` method — add `complete_simple()` as a new method, don't modify execute
- Existing test coverage — new tests are additive

-----

## Acceptance Criteria

1. **Tier 1 populates soul name and style.** User says "I'm John" → `soul.user_name` is "John" after the response. User says "keep it casual" → `soul.communication_style` is updated. Verified via `kernos-cli soul`.

2. **Tier 2 populates soul context and knowledge entries.** User says "I'm a plumber in Portland" → after Tier 2 async completes (~1-2s), `soul.user_context` contains "plumber" and "Portland". `kernos-cli knowledge` shows extracted facts with correct confidence levels and durability classifications.

3. **Zero additional user-perceived latency.** Tier 1 is sub-millisecond. Tier 2 is async (fire-and-forget after response sent). Response time is unchanged from 1B.5.

4. **Deduplication works via content_hash.** Same conversation processed twice produces the same knowledge entries, not duplicates. Content hash is stored on each entry. `get_knowledge_hashes()` returns a set used for O(1) dedup checks.

5. **Corrections work with supersedes chain.** User says "actually call me JT" after previously saying "I'm John" → old entry marked inactive, new entry has `supersedes` pointing to old entry's ID, `soul.user_name` updated to "JT".

6. **Confidence precedence.** A `stated` extraction overrides a previous `inferred` entry. An `inferred` extraction does NOT override a `stated` entry.

7. **Durability is classified.** "Is a plumber" gets `durability: "permanent"`. "Has appointment at 10am tomorrow" gets `durability: "session"` or `"expires_at:<ISO>"`. Field populated by Tier 2, not yet acted on.

8. **Name ask fires once on first interaction.** If user's first message doesn't include their name and the response doesn't already ask, "By the way — what should I call you?" is appended. Doesn't fire on subsequent messages. Doesn't double-ask if the LLM already asked.

9. **Bootstrap graduates.** After enough interactions with name, context, and style populated, the maturity gate passes, consolidation runs, `bootstrap_graduated` becomes True. Verified via `kernos-cli soul` showing `bootstrap: graduated`.

10. **Consolidation produces personality notes.** After graduation, `soul.personality_notes` contains a meaningful personality note (not empty, not the default). Verified via `kernos-cli soul`.

11. **Events are emitted.** `knowledge.extracted` events appear in the event stream for both Tier 1 (source: tier1_rules) and Tier 2 (source: tier2_llm). Bootstrap consolidation produces a reasoning call visible in events.

12. **Tier 2 failures are silent.** If the extraction LLM call fails, the user's response was already sent. No error visible to user. Logged internally. Tier 1 results are unaffected.

13. **`complete_simple()` works.** ReasoningService has a stateless completion method used by both Tier 2 extraction and bootstrap consolidation. No tools, no conversation history, no task events.

-----

## Live Verification

### Prerequisites
- KERNOS running on Discord
- Clean tenant (delete soul.json and conversations, or use fresh Discord account)

### Test Table

| Step | Action | Expected |
|---|---|---|
| 0 | Clean hatch — delete soul.json, knowledge.json, conversations, restart bot | Fresh start |
| 1 | Send: "Hey there" | Warm first meeting response WITH "By the way — what should I call you?" appended (name ask) |
| 2 | Send: "I'm John, I'm a plumber out in Portland" | Agent responds naturally, uses name |
| 3 | `kernos-cli soul <tenant_id>` | user_name: "John" (Tier 1 sync). communication_style still empty. |
| 4 | Wait 2 seconds, then `kernos-cli soul <tenant_id>` again | user_context contains "plumber" and "Portland" (Tier 2 async) |
| 5 | `kernos-cli knowledge <tenant_id>` | Shows extracted facts: plumber, Portland. Durability: permanent. Content hashes present. |
| 6 | Send: "keep it casual with me, I hate formal stuff" | Agent acknowledges naturally |
| 7 | `kernos-cli soul <tenant_id>` | communication_style: "casual" (Tier 1 sync) |
| 8 | Send: "actually, call me JT — everyone does" | Agent uses "JT" going forward |
| 9 | `kernos-cli soul <tenant_id>` | user_name: "JT" (corrected via Tier 2) |
| 10 | `kernos-cli knowledge <tenant_id>` | Old "Name is John" entry shows [archived] with --all flag. New "JT" entry active with supersedes. |
| 11 | Continue chatting until interaction_count > 10 | Normal conversation, agent learns more |
| 12 | `kernos-cli soul <tenant_id>` | bootstrap: graduated, personality_notes populated |
| 13 | `kernos-cli events <tenant_id> --limit 20` | Shows knowledge.extracted events (both tier1_rules and tier2_llm sources) |
| 14 | Restart bot, send a message | Agent knows you're JT, personality consistent, no re-bootstrap |
| 15 | Send same message as step 2 again, check knowledge count | No duplicate entries (content_hash dedup working) |

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Two tiers, independent | Rule-based sync + LLM async | If Tier 2 fails, Tier 1 still captures name and style. No all-or-nothing dependency. |
| Tier 1 scope is narrow | Name and style only, no context | "I'm a plumber" vs "my buddy's a plumber" can't be distinguished by regex. Wrong context in the soul is worse than no context. |
| Tier 2 via complete_simple() | New stateless method on ReasoningService | Extraction is kernel infrastructure, not agent reasoning. No tools, no task events, no conversation history. |
| Durability on KnowledgeEntry | "permanent" / "session" / "expires_at:\<ISO\>" | Distinguish "is a plumber" from "has appointment at 10am." Cheap to add now, expensive to retrofit. Phase 2 decay logic uses this. |
| Content hash for dedup | SHA256 of tenant_id + subject + content | O(1) dedup check via hash set. Idempotent by design. Same conversation processed twice = same result. |
| Supersedes chain for corrections | New entry points to old entry's ID | Provenance is navigable without versioning complexity. Old entry stays in store (inactive), never deleted. |
| Name ask on first interaction | Append "what should I call you?" if name unknown | Collapses most important maturity signal to one exchange. Conditional — doesn't double-ask if LLM already asked. |
| Confidence precedence | Stated overrides inferred, never reverse | "I'm a plumber" (stated) should not be overwritten by an inference. Stated = user's words. |
| Graduate regardless of consolidation | Consolidation is ideal, not required | If the LLM call fails, the soul still has enough substance. Retrying every interaction risks failure loops. |
| Projectors package | kernos/kernel/projectors/ with coordinator, rules, llm_extractor | Clean separation. Rules and LLM extraction are independent modules. Coordinator orchestrates. |
