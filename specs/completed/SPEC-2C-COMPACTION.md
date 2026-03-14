# SPEC-2C: Context Space Compaction System

**Status:** READY FOR REVIEW
**Depends on:** SPEC-2B-v2 Context Space Routing (complete)
**Design source:** KERNOS Compaction System Design Spec v1.2 (founder + Kit)
**Objective:** Replace simple truncation with a two-layer compaction system that preserves conversational knowledge across the context window boundary. Per-space compaction documents accumulate history through append-only Ledger entries and a rewritten Living State, eliminating summary drift.

**What changes for the user:** Nothing visible. But the agent's memory within a space gets dramatically better. Instead of oldest messages silently dropping off, the agent carries a structured historical record that grows richer over time. A D&D campaign session from two months ago is still there — compressed but not lost.

**What changes architecturally:** Each context space gains a compaction document (Ledger + Living State), a compaction state object, and an archive chain with index. The handler's `_assemble_space_context()` is rewritten to use the compaction document instead of `_truncate_to_budget()`. A Haiku-class LLM call fires when accumulated new messages exceed a pre-computed token ceiling.

**What this is NOT:**
- Not retrieval across archives (the index enables this, but the retrieval function is a future spec)
- Not cross-context retrieval (querying one space's history from another space)
- Not the `remember` tool (that builds on top of compaction)
- Not the NL Contract Parser (separate spec)

-----

## Component 1: Provider Token Adapter

**New file:** `kernos/kernel/tokens.py`

All token measurement flows through a provider adapter. The compaction system never references provider-specific API field names directly.

```python
class TokenAdapter:
    """Provider-agnostic token counting.

    Abstracts token measurement so the compaction system, trigger mechanism,
    and budget calculations work regardless of LLM provider.
    """

    async def count_tokens(self, text: str) -> int:
        """Count tokens for the given text. Provider-specific implementation."""
        ...
```

### Anthropic implementation

```python
class AnthropicTokenAdapter(TokenAdapter):
    """Uses Anthropic's free /v1/messages/count_tokens endpoint.

    Creates no message. Returns the exact count for the model's tokenizer.
    """

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    async def count_tokens(self, text: str) -> int:
        response = self.client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        )
        return response.input_tokens
```

### Fallback implementation

```python
class EstimateTokenAdapter(TokenAdapter):
    """Character-based estimation with 20% safety buffer.

    Biases toward early compaction, which is the safe direction.
    """

    async def count_tokens(self, text: str) -> int:
        import math
        return math.ceil(len(text) / 4 * 1.2)
```

The adapter is instantiated once in the handler's `__init__` and passed to the compaction service.

-----

## Component 2: Compaction State

**New file:** `kernos/kernel/compaction.py` (state and service in one module)

Each context space tracks its compaction state in a lightweight JSON object.

```python
@dataclass
class CompactionState:
    space_id: str
    history_tokens: int = 0         # Token count of active compaction document (re-counted)
    compaction_number: int = 0      # Sequential within current rotation
    global_compaction_number: int = 0  # Sequential across all rotations (never resets)
    archive_count: int = 0          # Number of archived documents
    message_ceiling: int = 0        # Pre-computed max new message tokens before trigger
    document_budget: int = 0        # Max tokens for the active document (derived)
    conversation_headroom: int = 0  # Protected conversation space
    cumulative_new_tokens: int = 0  # Running accumulator since last compaction
    last_compaction_at: str = ""    # ISO timestamp
    index_tokens: int = 0           # Token count of compaction index
    _context_def_tokens: int = 0    # Token count of space definition (measured once, re-measured on description update)
    _system_overhead: int = 0       # Token count of system prompt (approximate, for budget calculation)
```

### Constants

```python
# Conversation model
MODEL_MAX_TOKENS = 200_000  # Claude Sonnet context window

# Compaction model (Haiku-class)
COMPACTION_MODEL_MAX_TOKENS = 200_000
COMPACTION_OUTPUT_RESERVE_FRACTION = 0.20  # 20% reserved for generation
COMPACTION_MODEL_USABLE_TOKENS = int(
    COMPACTION_MODEL_MAX_TOKENS * (1 - COMPACTION_OUTPUT_RESERVE_FRACTION)
)  # 160,000

COMPACTION_INSTRUCTION_TOKENS = 2000  # Approximate size of the compaction prompt
```

### Ceiling computation

```python
def _compute_ceiling(self, comp_state: CompactionState) -> int:
    """Compute message ceiling — max new tokens before compaction fires.

    ceiling = compaction_model_usable_tokens
              - instruction_tokens
              - context_def_tokens (estimated, stable per space)
              - history_tokens (re-counted after last compaction)
    """
    return (
        COMPACTION_MODEL_USABLE_TOKENS
        - COMPACTION_INSTRUCTION_TOKENS
        - comp_state._context_def_tokens  # Measured once at space creation
        - comp_state.history_tokens
    )
```

Note: `_context_def_tokens` is the token count of the context space definition (name, type, description, posture). Measured via `adapter.count_tokens()` once at space creation and stored on the CompactionState. It's stable — only changes on session exit when the description updates, at which point it's re-measured.

### Storage

Per-space compaction files in the tenant's state directory:

```
{data_dir}/{tenant_id}/state/
    compaction/
        {space_id}/
            state.json              # CompactionState
            active_document.md      # The active Ledger + Living State
            index.md                # Compaction index (once archives exist)
            archives/
                compaction_archive_001.md
                compaction_archive_002.md
                ...
```

-----

## Component 3: Conversation Headroom Estimation

At context space creation, a lightweight LLM call estimates `conversation_headroom` — how many tokens of recent conversation history this domain needs for coherent interaction.

```python
HEADROOM_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "estimated_tokens_per_exchange": {"type": "integer"},
        "minimum_recent_exchanges": {"type": "integer"},
        "conversation_headroom": {"type": "integer"},
    },
    "required": ["reasoning", "estimated_tokens_per_exchange",
                  "minimum_recent_exchanges", "conversation_headroom"],
    "additionalProperties": False
}

async def estimate_headroom(
    reasoning: ReasoningService, space: ContextSpace
) -> int:
    """Estimate conversation headroom for a new context space."""

    result = await reasoning.complete_simple(
        system_prompt=(
            "Estimate how much token space this context space needs for "
            "recent conversation history to maintain coherent, useful interaction. "
            "Consider: how verbose are typical exchanges in this domain? "
            "How many recent turns does meaningful conversation require? "
            "A D&D campaign with long narrative exchanges needs more headroom "
            "than a scheduling space with short structured messages. "
            "Return estimated_tokens_per_exchange (average tokens per user+assistant pair), "
            "minimum_recent_exchanges (how many recent exchanges the agent needs to see), "
            "and conversation_headroom (their product, rounded up to nearest 1000)."
        ),
        user_content=(
            f"Space name: {space.name}\n"
            f"Space type: {space.space_type}\n"
            f"Space description: {space.description}\n"
        ),
        output_schema=HEADROOM_SCHEMA,
        max_tokens=256,
        prefer_cheap=True,
    )

    parsed = json.loads(result)
    headroom = parsed.get("conversation_headroom", 8000)

    # Clamp to reasonable range
    return max(4000, min(headroom, 40000))
```

The document budget is then derived:

```python
def compute_document_budget(
    model_max_tokens: int,
    system_overhead_tokens: int,
    index_tokens: int,
    conversation_headroom: int,
) -> int:
    """Derive document budget from what's left after non-negotiable space."""
    return model_max_tokens - system_overhead_tokens - index_tokens - conversation_headroom
```

The daily space gets a default headroom (8000 tokens) without an LLM call — daily conversations are general and don't need domain-specific estimation.

-----

## Component 4: The Compaction Service

**In:** `kernos/kernel/compaction.py`

### The trigger check

Runs after every conversation exchange. One comparison.

```python
class CompactionService:
    def __init__(
        self, state: StateStore, reasoning: ReasoningService,
        token_adapter: TokenAdapter, data_dir: str,
    ):
        self.state = state
        self.reasoning = reasoning
        self.adapter = token_adapter
        self.data_dir = Path(data_dir)

    async def should_compact(self, space_id: str, comp_state: CompactionState) -> bool:
        """Check if accumulated new messages exceed the ceiling."""
        return comp_state.cumulative_new_tokens >= comp_state.message_ceiling

    async def track_tokens(self, comp_state: CompactionState, new_tokens: int) -> None:
        """Increment the token accumulator after each exchange."""
        comp_state.cumulative_new_tokens += new_tokens
```

### Running compaction

```python
    async def compact(
        self, tenant_id: str, space_id: str, space: ContextSpace,
        new_messages: list[dict], comp_state: CompactionState,
    ) -> CompactionState:
        """Run one compaction cycle.

        Sends: instructions + context space definition + existing document + new messages
        Gets back: updated document (Ledger entries unchanged, new entry appended, Living State rewritten)
        """
        space_dir = self._space_dir(tenant_id, space_id)
        space_dir.mkdir(parents=True, exist_ok=True)

        # Load existing document (empty string on first run)
        active_doc_path = space_dir / "active_document.md"
        existing_doc = active_doc_path.read_text() if active_doc_path.exists() else ""

        # Format new messages
        messages_text = self._format_messages(new_messages)

        # Context space definition (the editorial lens)
        space_definition = (
            f"Space: {space.name}\n"
            f"Type: {space.space_type}\n"
            f"Description: {space.description}\n"
            f"Posture: {space.posture}\n"
        )

        # Build the compaction call
        user_content = ""
        if existing_doc:
            user_content += f"Previous Compaction History:\n\n{existing_doc}\n\n---\n\n"
        user_content += f"New Message Exchanges:\n\n{messages_text}"

        updated_doc = await self.reasoning.complete_simple(
            system_prompt=COMPACTION_SYSTEM_PROMPT,
            user_content=(
                f"Context Space Definition:\n\n{space_definition}\n\n---\n\n"
                f"{user_content}"
            ),
            max_tokens=16000,  # No artificial cap — let the model write what it needs
            prefer_cheap=True,
        )

        # Store updated document
        active_doc_path.write_text(updated_doc)

        # Re-count tokens via adapter (ground truth, not model's output_tokens)
        new_history_tokens = await self.adapter.count_tokens(updated_doc)

        # Update state
        comp_state.history_tokens = new_history_tokens
        comp_state.compaction_number += 1
        comp_state.global_compaction_number += 1
        comp_state.cumulative_new_tokens = 0
        comp_state.last_compaction_at = _now_iso()

        # Recompute message ceiling
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        # Check rotation
        if new_history_tokens > comp_state.document_budget:
            await self._rotate(tenant_id, space_id, space, comp_state)

        # Save state
        await self._save_state(tenant_id, space_id, comp_state)

        return comp_state
```

### The compaction prompt

This is the full system prompt sent to the compaction model. It incorporates the v1.2 design document's prompt with two additions addressing Kit's review concerns: a **minimum resolution floor** and explicit **clarity guidance** for ambiguous editorial calls.

```python
COMPACTION_SYSTEM_PROMPT = """You are the context historian for this context space. Your sole function is to maintain a living historical record that preserves everything a future instance of this conversation would need to continue seamlessly — as if nothing were ever lost.

---

#### Inputs

You receive the following inputs in this order:

1. These instructions.
2. Context Space Definition — the domain, purpose, and character of this context space. This is your editorial lens. It determines what matters, what to preserve in detail, what to condense, and what to discard.
3. Previous Compaction History — the existing historical record from prior compaction cycles (empty on first run). This document contains two layers: a Ledger followed by a Living State. Both are described below.
4. New Message Exchanges — the raw conversation messages since the last compaction that must now be integrated into the record.

This ordering is intentional. The Ledger occupies the middle of context where archival material belongs — present but not competing for attention. The Living State sits closer to the new messages, keeping current operational reality in sharp focus as you process what is new and produce your output.

---

#### Output

Return the complete, updated compaction history document structured as follows:

```
# Ledger
[All existing entries unchanged, new entry appended at end]

# Living State
[Rewritten to reflect current reality as of the end of the new message exchanges]
```

The Ledger comes first. The Living State comes last. This ensures that when this document is fed back into the next compaction cycle, the Living State will again sit closest to the new messages — maintaining optimal positioning across every cycle.

---

#### The Two Layers

**Ledger**

The Ledger is the immutable, append-only historical record. Each compaction cycle adds a new entry at the end. Once a Ledger entry is written, it is never edited, rewritten, or removed by future compaction cycles.

The Ledger is insurance. It guarantees that information which ages out of the Living State is never truly lost. It provides the historical depth necessary to understand not just what is true now, but how things got here.

Each Ledger entry captures what the new message exchanges contributed to this context space, written at the appropriate resolution for the domain.

The context space definition determines the character of each Ledger entry:
- In a transactional/operational context, Ledger entries read like detailed records — dated transaction logs, client interaction notes, specific data points (amounts, names, outcomes, exceptions).
- In a narrative/creative context, Ledger entries read like annotated footnotes to a novel — what happened during this session, which characters were involved, what changed in the world, what emotional beats landed, what plot threads advanced or closed.
- In a technical/engineering context, Ledger entries read like changelog entries — what was built, what was decided, what was tried and failed, what files or systems were touched.

Ledger entry format:

```
## Compaction #N — [first message timestamp] → [last message timestamp]

[Body of the entry, written at domain-appropriate resolution]
```

Ledger rules:
1. Append only. New entries are added at the end. Existing entries are never modified.
2. Self-contained. Each entry should be interpretable on its own, without requiring the reader to have read prior entries.
3. Domain-resolution. Write each entry at the level of detail the domain demands. A financial ledger preserves dollar amounts to the cent. A narrative ledger preserves character motivations and story beats. A technical ledger preserves file paths and error messages.
4. Exception capture. If anything anomalous occurred during this compaction window — something that deviates from the normal pattern — note it explicitly. This is the safety net for information that might not fit the Living State's structure.
5. Sequential numbering. Number entries sequentially starting from 1.
6. Minimum resolution floor. Every Ledger entry must preserve at minimum: all named entities mentioned, all decisions made or commitments given, all facts that would change behavior if forgotten, and any content the user explicitly asked to be remembered. When in doubt about whether a detail meets this bar, include it — the cost of an extra sentence is lower than the cost of a lost detail.

**Living State**

The Living State is the mutable, current-truth layer. It represents what is true, active, and relevant right now. It is rewritten on every compaction cycle to reflect the latest reality.

A reader who only reads the Living State should be able to step into this conversation and operate competently — understanding what is happening, who is involved, what has been decided, and what is pending.

The Living State is not a summary of the conversation. It is a maintained snapshot of current reality as understood through this context space's domain. Old information that is no longer active does not persist here — it lives in the Ledger.

The context space definition determines the character of the Living State:
- In a transactional/operational context, the Living State reads like a dashboard — specific, data-dense, covering recent activity at full fidelity.
- In a narrative/creative context, the Living State reads like the current chapter — who is where, what just happened, what tensions are active, what is about to happen.
- In a technical/engineering context, the Living State reads like a working document — current architecture, active problems, recent decisions, open threads.

Organize the Living State into whatever sections serve the domain. Suggested sections (adapt freely):
- Current Situation — What is happening right now.
- Active Entities — People, systems, characters, accounts currently in play.
- Open Items — Unresolved questions, pending tasks, things deferred.
- Recent Decisions — What has been decided recently and why, if the reasoning still matters.

---

#### How to Operate

First compaction (no previous history):

1. Write Ledger entry Compaction #1 capturing the full scope of what transpired.
2. Construct the initial Living State from the new message exchanges.
3. Return the complete document: Ledger then Living State.

Subsequent compactions:

1. Pass through all existing Ledger entries unchanged. Do not touch them.
2. Write a new Ledger entry. Append it after the last existing entry. Capture what the new message exchanges contributed, at domain-appropriate resolution.
3. Rewrite the Living State. Update it to reflect current reality as of the end of the new message exchanges. Remove what is no longer active. Add what is new. Information aging out of the Living State is not lost — it already exists in prior Ledger entries.
4. Return the complete document — full Ledger (all prior entries unchanged, new entry appended), followed by the updated Living State.

---

#### Domain-Aware Judgment

The context space definition tells you what this space is for. Use it as your editorial lens:

What belongs in the Living State — anything a participant needs to know to continue operating right now.

What resolution for the Ledger — match the domain. Transactional domains need data-point fidelity. Narrative domains need story-beat fidelity. Technical domains need implementation-detail fidelity.

What to discard entirely — mechanical exchanges, redundant restatements, thinking-out-loud that led nowhere, greetings, acknowledgments. Information with zero retrieval value in either layer.

What to promote from noise to signal — sometimes an exchange that would normally be discarded carries unusual weight. An offhand remark that reveals a constraint. An emotional reaction in a transactional context. A mechanical detail in a narrative context. The context space definition should help you recognize these.

When ambiguous — err toward preservation. If you are unsure whether a detail belongs in the Ledger entry, include it. If you are unsure whether something is still active enough for the Living State, keep it one more cycle and let the next compaction decide. The cost of an extra sentence is always lower than the cost of a lost detail. Never resolve ambiguity by discarding.

---

#### Rules

1. Never fabricate. Every fact in either layer must originate from the message exchanges or the prior compaction history.
2. Living State is rewritten freely. It reflects current truth. Old states are preserved in the Ledger.
3. Ledger entries are immutable. Once written, never edited, merged, reworded, or removed.
4. Preserve specificity in both layers. Names, numbers, identifiers, exact phrasing of commitments — these survive compaction at full fidelity wherever they appear.
5. The document must be self-contained. A future reader with no access to the original messages should be able to continue the conversation from the Living State and reconstruct any historical moment from the Ledger.
6. Number Ledger entries sequentially.
7. Every Ledger entry carries a message date range header regardless of domain."""
```

**What the prompt adds over v1.2 (addressing Kit's review):**

- **Minimum resolution floor** (Ledger rule #6): Explicit list of what every entry MUST preserve regardless of domain — named entities, decisions, commitments, behavior-changing facts, explicit remember-requests. This prevents the compaction model from making lossy editorial calls on content that has clear retrieval value.

- **Ambiguity resolution** (in "Domain-Aware Judgment" section): "When ambiguous — err toward preservation." Plus specific guidance: unsure about Ledger inclusion → include it. Unsure about Living State removal → keep it one more cycle. "Never resolve ambiguity by discarding." This creates a consistent bias direction that prevents the most common compaction failure mode (silent detail loss).

-----

## Component 5: Document Rotation & Archival

When the active compaction document exceeds the document budget after a compaction cycle, it rotates.

```python
    async def _rotate(
        self, tenant_id: str, space_id: str,
        space: ContextSpace, comp_state: CompactionState,
    ) -> None:
        """Seal active document as archive, create fresh document."""
        space_dir = self._space_dir(tenant_id, space_id)
        archive_dir = space_dir / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)

        active_doc_path = space_dir / "active_document.md"
        active_doc = active_doc_path.read_text()

        # 1. Seal as archive
        comp_state.archive_count += 1
        archive_name = f"compaction_archive_{comp_state.archive_count:03d}.md"
        (archive_dir / archive_name).write_text(active_doc)

        # 2. Generate index summary
        # Use the end of the document (Living State + recent Ledger entries)
        # which is more representative than the oldest entries at the beginning
        summary_input = active_doc[-8000:] if len(active_doc) > 8000 else active_doc
        summary = await self.reasoning.complete_simple(
            system_prompt=(
                "Write a 1-3 sentence summary of this compaction document. "
                "Include the domain, key events, and date range. "
                "This summary will be used as an index entry for future retrieval."
            ),
            user_content=summary_input,
            max_tokens=200,
            prefer_cheap=True,
        )

        # 3. Append to index
        index_path = space_dir / "index.md"
        first_compaction = comp_state.global_compaction_number - comp_state.compaction_number + 1
        last_compaction = comp_state.global_compaction_number
        index_entry = (
            f"\n## Archive #{comp_state.archive_count}\n"
            f"Compactions {first_compaction}–{last_compaction} | "
            f"{comp_state.last_compaction_at[:10]}\n\n"
            f"{summary.strip()}\n"
        )

        if index_path.exists():
            existing_index = index_path.read_text()
            index_path.write_text(existing_index + index_entry)
        else:
            index_path.write_text("# Compaction Index\n" + index_entry)

        # 4. Re-count index tokens
        comp_state.index_tokens = await self.adapter.count_tokens(
            index_path.read_text()
        )

        # 5. Create new active document
        # Extract Living State from the archived document
        living_state = self._extract_living_state(active_doc)

        # Carry forward-relevant Ledger entries (last 2 compaction cycles)
        forward_entries = self._extract_forward_relevant_entries(
            active_doc, comp_state.compaction_number
        )

        new_doc = ""
        if forward_entries:
            new_doc += f"# Ledger\n\n{forward_entries}\n\n"
        else:
            new_doc += "# Ledger\n\n"
        new_doc += f"# Living State\n\n{living_state}"

        active_doc_path.write_text(new_doc)

        # 6. Re-count and recompute
        comp_state.history_tokens = await self.adapter.count_tokens(new_doc)
        comp_state.compaction_number = 0  # Reset rotation-local counter
        comp_state.document_budget = compute_document_budget(
            MODEL_MAX_TOKENS,
            comp_state._system_overhead,
            comp_state.index_tokens,
            comp_state.conversation_headroom,
        )
        comp_state.message_ceiling = self._compute_ceiling(comp_state)
```

### Forward-relevance determination (Kit's concern #2)

On rotation, recent Ledger entries that are still actively relevant carry forward into the new document. The v1 implementation uses a simple, concrete rule:

**The last 2 compaction cycles' Ledger entries carry forward.**

Rationale: entries from the most recent compaction cycles are the ones most likely to contain active threads — decisions in progress, recently mentioned entities, open items. Two cycles provides enough bridge context without carrying forward the entire Ledger (which would defeat rotation).

```python
    def _extract_forward_relevant_entries(
        self, document: str, current_compaction_number: int,
    ) -> str:
        """Extract the last 2 Ledger entries for carry-forward on rotation.

        Simple rule: the two most recent compaction entries are carried.
        These bridge the gap between the archived history and the Living State,
        preventing the jarring context loss that would occur if rotation
        started with only the Living State.

        Future refinement: LLM judgment call at rotation time to select
        which entries are still actively relevant. For v1, recency is
        the proxy for relevance.
        """
        # Parse Ledger entries from the document
        entries = self._parse_ledger_entries(document)
        if not entries:
            return ""

        # Take the last 2
        forward = entries[-2:]
        return "\n\n".join(forward)

    def _parse_ledger_entries(self, document: str) -> list[str]:
        """Split the Ledger section into individual entries."""
        import re
        # Find the Ledger section (between "# Ledger" and "# Living State")
        ledger_match = re.search(
            r'# Ledger\s*\n(.*?)(?=# Living State)', document, re.DOTALL
        )
        if not ledger_match:
            return []

        ledger_text = ledger_match.group(1)
        # Split on "## Compaction #N" headers
        entries = re.split(r'(?=## Compaction #\d+)', ledger_text)
        return [e.strip() for e in entries if e.strip()]

    def _extract_living_state(self, document: str) -> str:
        """Extract the Living State section from a compaction document."""
        import re
        match = re.search(r'# Living State\s*\n(.*)', document, re.DOTALL)
        return match.group(1).strip() if match else ""
```

-----

## Component 6: Context Assembly Integration

**Modified file:** `kernos/messages/handler.py`

Replace `_truncate_to_budget()` with compaction-aware assembly.

### New context assembly

```python
async def _assemble_space_context(
    self, tenant_id: str, conversation_id: str,
    active_space_id: str, active_space: ContextSpace | None,
) -> tuple[list[dict], str | None]:
    """Assemble the agent's conversation context for the active space.

    Context window layout (top to bottom):
    [System prompt]                    ← primacy zone
    [Context space definition]         ← primacy zone
    [Compaction index] (if exists)     ← historical awareness
    [Cross-domain injections]          ← background, low attention
    [Compaction document]
      ├─ Ledger (oldest → newest)      ← middle zone (archival)
      └─ Living State                  ← approaching recency zone
    [Recent conversation messages]     ← strongest recency zone

    Returns (recent_messages, system_prefix) where:
    - recent_messages: messages since last compaction (the live thread)
    - system_prefix: cross-domain injections + compaction doc + index to
      inject into the system prompt
    """
    prefix_parts = []

    # 1. Compaction index (if exists) — historical awareness
    comp_state = await self.compaction.load_state(tenant_id, active_space_id)
    if comp_state and comp_state.index_tokens > 0:
        index_text = await self.compaction.load_index(tenant_id, active_space_id)
        if index_text:
            prefix_parts.append(
                f"## Archived history (summaries — full archives available on request):\n"
                f"{index_text}"
            )

    # 2. Cross-domain injections — last 5 turns from other spaces
    cross = await self.conversations.get_cross_domain_messages(
        tenant_id, conversation_id, active_space_id,
        last_n_turns=CROSS_DOMAIN_INJECTION_TURNS,
    )
    if cross:
        lines = []
        for msg in cross:
            role_label = "You" if msg["role"] == "assistant" else "User"
            ts = msg.get("timestamp", "")
            content = str(msg.get("content", ""))[:300]
            lines.append(f"[{role_label}, {ts}]: {content}")
        prefix_parts.append(
            f"## Recent activity in other areas (background — read but do not dwell on):\n"
            + "\n".join(lines)
        )

    # 3. Compaction document (Ledger → Living State)
    active_doc = await self.compaction.load_document(tenant_id, active_space_id)
    if active_doc:
        prefix_parts.append(
            f"## Context history for this space:\n{active_doc}"
        )

    system_prefix = "\n\n".join(prefix_parts) if prefix_parts else None

    # 4. Recent messages since last compaction (the live thread)
    # These are the messages the compaction model hasn't processed yet
    is_daily = active_space.is_default if active_space else False
    thread = await self.conversations.get_space_thread(
        tenant_id, conversation_id, active_space_id,
        max_messages=50,
        include_untagged=is_daily,
    )

    # Only include messages since the last compaction
    if comp_state and comp_state.last_compaction_at:
        thread = [
            m for m in thread
            if m.get("timestamp", "") > comp_state.last_compaction_at
        ]

    return thread, system_prefix
```

### System prompt integration

The `_build_system_prompt()` function already accepts `cross_domain_prefix` and injects it at position 0. The compaction document, index, and cross-domain injections are all included in this prefix. The ordering in the prefix follows the attention-optimized layout from the design spec.

-----

## Component 7: Handler Integration

### Token tracking after each exchange

After each conversation exchange, the handler tracks new tokens and checks the compaction trigger.

```python
# In handler.process(), after reasoning and storing the assistant response:

# Track tokens for compaction trigger
if comp_state:
    # Estimate tokens from this exchange (user message + assistant response)
    exchange_tokens = await self.compaction.adapter.count_tokens(
        message.content + "\n" + response_text
    )
    comp_state.cumulative_new_tokens += exchange_tokens

    # Check compaction trigger
    if await self.compaction.should_compact(active_space_id, comp_state):
        # Get messages since last compaction
        new_messages = [
            m for m in space_thread_full
            if m.get("timestamp", "") > (comp_state.last_compaction_at or "")
        ]
        comp_state = await self.compaction.compact(
            tenant_id, active_space_id, active_space, new_messages, comp_state
        )
```

### CompactionService initialization

The CompactionService is created in the handler's `__init__` alongside the router:

```python
def __init__(self, mcp, conversations, tenants, audit, events,
             state, reasoning, registry, engine):
    # ... existing init ...
    self.compaction = CompactionService(
        state=state,
        reasoning=reasoning,
        token_adapter=AnthropicTokenAdapter(api_key=os.getenv("ANTHROPIC_API_KEY", "")),
        data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
    )
```

### Space creation hook — headroom estimation

When Gate 2 creates a new space, compute conversation headroom:

```python
# In _trigger_gate2(), after creating the new ContextSpace:

# Estimate conversation headroom for the new space
headroom = await estimate_headroom(self.reasoning, new_space)

# Measure fixed token costs
context_def = (
    f"Space: {new_space.name}\nType: {new_space.space_type}\n"
    f"Description: {new_space.description}\nPosture: {new_space.posture}\n"
)
context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
system_overhead = await self.compaction.adapter.count_tokens(
    _build_system_prompt(...)  # Approximate — the prompt for this space
)

# Initialize compaction state
doc_budget = compute_document_budget(
    MODEL_MAX_TOKENS, system_overhead, 0, headroom
)
comp_state = CompactionState(
    space_id=new_space.id,
    conversation_headroom=headroom,
    document_budget=doc_budget,
    message_ceiling=min(
        doc_budget,
        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - context_def_tokens,
    ),  # Clamp: first compaction has no existing document, so ceiling is
        # bounded by the compaction model's window minus fixed inputs.
        # After first compaction, _compute_ceiling() takes over.
    _context_def_tokens=context_def_tokens,
    _system_overhead=system_overhead,
)
await self.compaction.save_state(tenant_id, new_space.id, comp_state)
```

For the daily space: initialize compaction state with `conversation_headroom=8000` at tenant provisioning time. No LLM call needed.

-----

## Component 8: Adaptive Headroom (Lightweight)

Rotation frequency provides feedback for tuning headroom. After each rotation:

```python
# In _rotate(), after creating the new document:

# Adaptive headroom: track rotation frequency
rotations_per_100_compactions = (
    comp_state.archive_count /
    max(comp_state.global_compaction_number, 1)
) * 100

if rotations_per_100_compactions > 20:
    # Rotating too frequently — headroom might be too large (document budget too small)
    # Reduce headroom by 5% to give the document more room
    comp_state.conversation_headroom = int(comp_state.conversation_headroom * 0.95)

# NOTE: We do NOT adjust in the other direction. A space that never rotates
# is not a problem — it means the domain is sparse or lightly used. Low-volume
# spaces should keep their full document budget, not get penalized with
# artificial rotation from shrinking it.

# Clamp to bounds
comp_state.conversation_headroom = max(4000, min(comp_state.conversation_headroom, 40000))

# Recompute budget
comp_state.document_budget = compute_document_budget(...)
```

This is gentle tuning — 5% adjustments based on observed rotation frequency. Not a radical rewrite. The system finds its own equilibrium over time.

-----

## Implementation Order

1. **TokenAdapter** — provider adapter interface + Anthropic implementation + fallback
2. **CompactionState** — dataclass + JSON persistence (load/save in compaction directory)
3. **CompactionService** — trigger check, compact(), document parsing helpers
4. **Compaction prompt** — the full system prompt as a constant
5. **Rotation** — archive sealing, index summary generation, Living State carry-forward, forward-relevant entry extraction
6. **Headroom estimation** — LLM call at space creation, default for daily
7. **Context assembly rewrite** — replace `_truncate_to_budget()` with compaction-aware assembly
8. **Handler integration** — token tracking, trigger check after each exchange, compaction service init
9. **Adaptive headroom** — rotation frequency tracking and adjustment
10. **CLI additions** — `kernos-cli compaction <tenant_id> <space_id>` showing state, document size, archive count
11. **Tests** — trigger mechanism, compaction call (mock LLM), rotation, index generation, forward-relevance extraction, context assembly, headroom estimation, adaptive tuning

-----

## What Claude Code MUST NOT Change

- Router logic (2B-v2)
- Entity resolution pipeline (2A)
- Fact dedup pipeline (2A)
- Tier 2 extraction pipeline
- CovenantRule schema
- Soul data model
- ContextSpace model (only add compaction-related fields if needed)
- Template content
- Gate 1/Gate 2 logic (except the headroom estimation hook on Gate 2)

-----

## Acceptance Criteria

1. **First compaction fires correctly.** After enough messages to exceed the ceiling, a compaction cycle runs. The active document contains a Ledger with Compaction #1 and a Living State. Verified via `kernos-cli compaction`.

2. **Subsequent compactions append correctly.** Second compaction adds Compaction #2 to the Ledger. Existing Compaction #1 is unchanged (byte-identical). Living State is rewritten. Verified.

3. **Token trigger is accurate.** Compaction fires when cumulative new tokens exceed the pre-computed ceiling. Not before. Verified by inspecting CompactionState.cumulative_new_tokens vs message_ceiling.

4. **Re-count uses adapter, not model output_tokens.** history_tokens is set by `adapter.count_tokens()` on the stored document, not from the API response. Verified by checking the CompactionState after compaction.

5. **Rotation works.** When the active document exceeds the budget, it seals as an archive, index entry is appended, new document starts with Living State + last 2 Ledger entries. Verified.

6. **Index injected into conversation model calls.** After rotation, every subsequent conversation model call includes the compaction index in the system prompt prefix. Verified by inspecting the assembled prompt.

7. **Forward-relevant entries carry forward.** After rotation, the new active document contains the last 2 Ledger entries from the archived document. Verified.

8. **Context assembly uses compaction document.** The agent sees: [system prompt + index + cross-domain + compaction document + recent messages since last compaction]. Not the old truncated thread. Verified by inspecting what the reasoning service receives.

9. **Headroom estimation runs at space creation.** Gate 2 creates a space → headroom LLM call fires → CompactionState.conversation_headroom is set. Verified.

10. **Daily space gets default headroom.** No LLM call. conversation_headroom = 8000. Verified.

11. **Ledger entries have message date ranges.** Every entry header has `## Compaction #N — [start] → [end]` format. Verified.

12. **Minimum resolution floor holds.** Compaction preserves: named entities, decisions, commitments, behavior-changing facts, explicit remember-requests. Verified by sending messages containing these elements and inspecting the Ledger entry.

13. **Compaction document domain-adaptive.** D&D space produces narrative-style Ledger entries and a "current chapter" Living State. Business space produces transactional records and a dashboard. Verified by creating spaces of different types and comparing output.

14. **All existing tests pass.** New tests cover all compaction components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Prerequisites
- KERNOS running on Discord
- Test tenant with at least 2 spaces (daily + one domain space)
- Enough message history to trigger at least 2 compaction cycles

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send 15-20 messages in the D&D space | Messages accumulate. No compaction yet (below ceiling). |
| 2 | Check `kernos-cli compaction <tenant> <space>` | Shows CompactionState: cumulative_new_tokens increasing, compaction_number = 0. |
| 3 | Continue sending until ceiling is hit | Compaction fires. Active document created with Compaction #1 + Living State. |
| 4 | `kernos-cli compaction <tenant> <space>` | compaction_number = 1. history_tokens populated. message_ceiling recomputed. |
| 5 | Inspect the active document | Ledger entry has date range header. Living State reads like a "current chapter" (D&D domain). Named entities, plot points, character details preserved. |
| 6 | Send more messages, trigger compaction again | Compaction #2 appended. Compaction #1 byte-identical. Living State rewritten with current state. |
| 7 | Send: "What were we doing in the campaign?" | Agent responds using both the compaction document (history) and recent messages (current context). Should reference details from Compaction #1 era. |
| 8 | If budget allows, force rotation (lower document_budget temporarily) | Archive created. Index entry written. New active document has Living State + last 2 Ledger entries. |
| 9 | After rotation, send a message | System prompt includes the compaction index. Agent knows archived history exists. |
| 10 | Check D&D vs Daily compaction character | D&D: narrative entries, story-beat preservation. Daily: general entries, factual preservation. Different editorial voices from the same prompt. |

Write results to `tests/live/LIVE-TEST-2C.md`.

After live verification: update DECISIONS.md and docs/TECHNICAL-ARCHITECTURE.md.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Two-layer document (Ledger + Living State) | Not regenerative summarization | Anchored iterative accumulation eliminates summary drift. Factory.ai benchmark: structured 3.70/5.0 vs regenerative 3.35-3.44/5.0. |
| Ledger is append-only | Never edited, merged, or consolidated | Immutability prevents compounding information loss. Redundancy across entries is a lifecycle record, not duplication. Size managed by rotation, not editorial tightening. |
| Living State rewritten every cycle | Not accumulated | Current truth stays lean and precise. Old information lives in the Ledger. |
| Attention-optimized ordering | Ledger middle, Living State near bottom | "Lost in the Middle" (Liu et al.): LLMs attend most to primacy and recency positions. Living State adjacent to recent messages = high attention. Ledger in middle = present for reference, not competing. |
| Domain-aware editorial judgment | Via context space definition | The same prompt produces different document characters for D&D vs invoicing vs engineering. The definition is the editorial lens. |
| Minimum resolution floor | Named entities, decisions, commitments always preserved | Prevents the most common compaction failure: silent detail loss on content with clear retrieval value. |
| Ambiguity → preserve | Never resolve ambiguity by discarding | Consistent bias direction. An extra Ledger sentence costs nothing; a lost detail costs trust. |
| Provider adapter for token counting | Not provider-specific API coupling | Compaction system works regardless of provider. Anthropic's free count endpoint is optimal but not required. |
| Ground truth re-count | adapter.count_tokens() on stored document | Tokenization on re-ingestion may differ from generation-time counts. The re-count prevents trigger drift over hundreds of cycles. |
| Headroom derived, not fractioned | Conversation quality is non-negotiable | The document budget is what's left after protecting conversation space. Not the other way around. |
| Adaptive headroom from rotation frequency | One-directional: reduce headroom on too-frequent rotation only | Too-frequent rotation is a real problem (document can't accumulate). Never rotating is fine — sparse or low-volume spaces don't need correction. |
| Forward-relevant: last 2 entries | Not LLM judgment (v1) | Simple, concrete, predictable. Recency is a good proxy for relevance. LLM selection is a future refinement if needed. |
| Index in every conversation call | Once archives exist | Tiny footprint (~50-100 tokens per entry). Agent knows history exists and can reference what it broadly contains. |
| Compaction on every space, including daily | Daily is a real space | "What's a good recipe for pasta?" from two months ago matters when the user asks again. Daily isn't exempt from memory. |
