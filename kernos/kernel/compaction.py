"""Compaction Service — two-layer (Ledger + Living State) context compaction.

Per-space compaction documents accumulate history through append-only Ledger
entries and a rewritten Living State, eliminating summary drift. Replaces
simple truncation with structured historical preservation.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import StateStore
from kernos.kernel.tokens import TokenAdapter

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_MAX_TOKENS = 200_000  # Claude Sonnet context window

COMPACTION_MODEL_MAX_TOKENS = 200_000
COMPACTION_OUTPUT_RESERVE_FRACTION = 0.20
COMPACTION_MODEL_USABLE_TOKENS = int(
    COMPACTION_MODEL_MAX_TOKENS * (1 - COMPACTION_OUTPUT_RESERVE_FRACTION)
)  # 160,000

COMPACTION_INSTRUCTION_TOKENS = 2000

DEFAULT_DAILY_HEADROOM = 8000


# ---------------------------------------------------------------------------
# CompactionState
# ---------------------------------------------------------------------------


@dataclass
class CompactionState:
    """Per-space compaction state, persisted as JSON."""

    space_id: str
    history_tokens: int = 0
    compaction_number: int = 0
    global_compaction_number: int = 0
    archive_count: int = 0
    message_ceiling: int = 0
    document_budget: int = 0
    conversation_headroom: int = 0
    cumulative_new_tokens: int = 0
    last_compaction_at: str = ""
    index_tokens: int = 0
    _context_def_tokens: int = 0
    _system_overhead: int = 0


# ---------------------------------------------------------------------------
# Budget computation
# ---------------------------------------------------------------------------


def compute_document_budget(
    model_max_tokens: int,
    system_overhead_tokens: int,
    index_tokens: int,
    conversation_headroom: int,
) -> int:
    """Derive document budget from what's left after non-negotiable space."""
    return model_max_tokens - system_overhead_tokens - index_tokens - conversation_headroom


# ---------------------------------------------------------------------------
# Headroom estimation
# ---------------------------------------------------------------------------

HEADROOM_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "estimated_tokens_per_exchange": {"type": "integer"},
        "minimum_recent_exchanges": {"type": "integer"},
        "conversation_headroom": {"type": "integer"},
    },
    "required": [
        "reasoning",
        "estimated_tokens_per_exchange",
        "minimum_recent_exchanges",
        "conversation_headroom",
    ],
    "additionalProperties": False,
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
    headroom = parsed.get("conversation_headroom", DEFAULT_DAILY_HEADROOM)

    # Clamp to reasonable range
    return max(4000, min(headroom, 40000))


# ---------------------------------------------------------------------------
# Compaction prompt
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CompactionService
# ---------------------------------------------------------------------------


class CompactionService:
    """Manages compaction lifecycle for all context spaces."""

    def __init__(
        self,
        state: StateStore,
        reasoning: ReasoningService,
        token_adapter: TokenAdapter,
        data_dir: str,
        events: EventStream | None = None,
    ) -> None:
        self.state = state
        self.reasoning = reasoning
        self.adapter = token_adapter
        self.data_dir = Path(data_dir)
        self.events = events

    def _space_dir(self, tenant_id: str, space_id: str) -> Path:
        from kernos.utils import _safe_name
        return (
            self.data_dir
            / _safe_name(tenant_id)
            / "state"
            / "compaction"
            / _safe_name(space_id)
        )

    # --- Persistence ---

    async def load_state(
        self, tenant_id: str, space_id: str
    ) -> CompactionState | None:
        """Load CompactionState from disk. Returns None if not found."""
        state_path = self._space_dir(tenant_id, space_id) / "state.json"
        if not state_path.exists():
            return None
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return CompactionState(
                space_id=data.get("space_id", space_id),
                history_tokens=data.get("history_tokens", 0),
                compaction_number=data.get("compaction_number", 0),
                global_compaction_number=data.get("global_compaction_number", 0),
                archive_count=data.get("archive_count", 0),
                message_ceiling=data.get("message_ceiling", 0),
                document_budget=data.get("document_budget", 0),
                conversation_headroom=data.get("conversation_headroom", 0),
                cumulative_new_tokens=data.get("cumulative_new_tokens", 0),
                last_compaction_at=data.get("last_compaction_at", ""),
                index_tokens=data.get("index_tokens", 0),
                _context_def_tokens=data.get("_context_def_tokens", 0),
                _system_overhead=data.get("_system_overhead", 0),
            )
        except Exception as exc:
            logger.warning("Failed to load compaction state for %s/%s: %s", tenant_id, space_id, exc)
            return None

    async def save_state(
        self, tenant_id: str, space_id: str, comp_state: CompactionState
    ) -> None:
        """Persist CompactionState to disk."""
        space_dir = self._space_dir(tenant_id, space_id)
        space_dir.mkdir(parents=True, exist_ok=True)
        state_path = space_dir / "state.json"
        data = {
            "space_id": comp_state.space_id,
            "history_tokens": comp_state.history_tokens,
            "compaction_number": comp_state.compaction_number,
            "global_compaction_number": comp_state.global_compaction_number,
            "archive_count": comp_state.archive_count,
            "message_ceiling": comp_state.message_ceiling,
            "document_budget": comp_state.document_budget,
            "conversation_headroom": comp_state.conversation_headroom,
            "cumulative_new_tokens": comp_state.cumulative_new_tokens,
            "last_compaction_at": comp_state.last_compaction_at,
            "index_tokens": comp_state.index_tokens,
            "_context_def_tokens": comp_state._context_def_tokens,
            "_system_overhead": comp_state._system_overhead,
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def load_document(
        self, tenant_id: str, space_id: str
    ) -> str | None:
        """Load the active compaction document. Returns None if not found."""
        doc_path = self._space_dir(tenant_id, space_id) / "active_document.md"
        if not doc_path.exists():
            return None
        return doc_path.read_text(encoding="utf-8")

    async def load_index(
        self, tenant_id: str, space_id: str
    ) -> str | None:
        """Load the compaction index. Returns None if not found."""
        index_path = self._space_dir(tenant_id, space_id) / "index.md"
        if not index_path.exists():
            return None
        return index_path.read_text(encoding="utf-8")

    # --- Trigger ---

    async def should_compact(
        self, space_id: str, comp_state: CompactionState
    ) -> bool:
        """Check if accumulated new messages exceed the ceiling."""
        return comp_state.cumulative_new_tokens >= comp_state.message_ceiling

    # --- Ceiling ---

    def _compute_ceiling(self, comp_state: CompactionState) -> int:
        """Compute message ceiling — max new tokens before compaction fires."""
        return (
            COMPACTION_MODEL_USABLE_TOKENS
            - COMPACTION_INSTRUCTION_TOKENS
            - comp_state._context_def_tokens
            - comp_state.history_tokens
        )

    # --- Formatting ---

    def _format_messages(self, messages: list[dict]) -> str:
        """Format messages for the compaction prompt."""
        lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Agent"
            ts = msg.get("timestamp", "")
            content = msg.get("content", "")
            if ts:
                lines.append(f"[{role}, {ts}]: {content}")
            else:
                lines.append(f"[{role}]: {content}")
        return "\n\n".join(lines)

    # --- Document parsing ---

    def _parse_ledger_entries(self, document: str) -> list[str]:
        """Split the Ledger section into individual entries."""
        ledger_match = re.search(
            r'# Ledger\s*\n(.*?)(?=# Living State)', document, re.DOTALL
        )
        if not ledger_match:
            return []

        ledger_text = ledger_match.group(1)
        entries = re.split(r'(?=## Compaction #\d+)', ledger_text)
        return [e.strip() for e in entries if e.strip()]

    def _extract_living_state(self, document: str) -> str:
        """Extract the Living State section from a compaction document."""
        match = re.search(r'# Living State\s*\n(.*)', document, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _extract_forward_relevant_entries(
        self, document: str, current_compaction_number: int,
    ) -> str:
        """Extract the last 2 Ledger entries for carry-forward on rotation."""
        entries = self._parse_ledger_entries(document)
        if not entries:
            return ""
        forward = entries[-2:]
        return "\n\n".join(forward)

    # --- Compact ---

    async def compact(
        self,
        tenant_id: str,
        space_id: str,
        space: ContextSpace,
        new_messages: list[dict],
        comp_state: CompactionState,
    ) -> CompactionState:
        """Run one compaction cycle."""
        space_dir = self._space_dir(tenant_id, space_id)
        space_dir.mkdir(parents=True, exist_ok=True)

        # Emit compaction.triggered
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_TRIGGERED,
                    tenant_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "compaction_number": comp_state.global_compaction_number + 1,
                        "cumulative_new_tokens": comp_state.cumulative_new_tokens,
                        "message_ceiling": comp_state.message_ceiling,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.triggered: %s", exc)

        # Load existing document
        active_doc_path = space_dir / "active_document.md"
        existing_doc = active_doc_path.read_text(encoding="utf-8") if active_doc_path.exists() else ""

        # Format new messages
        messages_text = self._format_messages(new_messages)

        # Context space definition
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
            max_tokens=16000,
            prefer_cheap=True,
        )

        # Store updated document
        active_doc_path.write_text(updated_doc, encoding="utf-8")

        # Re-count tokens via adapter
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
        await self.save_state(tenant_id, space_id, comp_state)

        # Emit compaction.completed
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_COMPLETED,
                    tenant_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "compaction_number": comp_state.global_compaction_number,
                        "history_tokens": comp_state.history_tokens,
                        "message_ceiling": comp_state.message_ceiling,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.completed: %s", exc)

        return comp_state

    # --- Rotation ---

    async def _rotate(
        self,
        tenant_id: str,
        space_id: str,
        space: ContextSpace,
        comp_state: CompactionState,
    ) -> None:
        """Seal active document as archive, create fresh document."""
        space_dir = self._space_dir(tenant_id, space_id)
        archive_dir = space_dir / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)

        active_doc_path = space_dir / "active_document.md"
        active_doc = active_doc_path.read_text(encoding="utf-8")

        # 1. Seal as archive
        comp_state.archive_count += 1
        archive_name = f"compaction_archive_{comp_state.archive_count:03d}.md"
        (archive_dir / archive_name).write_text(active_doc, encoding="utf-8")

        # 2. Generate index summary
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
            f"Compactions {first_compaction}\u2013{last_compaction} | "
            f"{comp_state.last_compaction_at[:10]}\n\n"
            f"{summary.strip()}\n"
        )

        if index_path.exists():
            existing_index = index_path.read_text(encoding="utf-8")
            index_path.write_text(existing_index + index_entry, encoding="utf-8")
        else:
            index_path.write_text("# Compaction Index\n" + index_entry, encoding="utf-8")

        # 4. Re-count index tokens
        comp_state.index_tokens = await self.adapter.count_tokens(
            index_path.read_text(encoding="utf-8")
        )

        # 5. Create new active document
        living_state = self._extract_living_state(active_doc)
        forward_entries = self._extract_forward_relevant_entries(
            active_doc, comp_state.compaction_number
        )

        new_doc = ""
        if forward_entries:
            new_doc += f"# Ledger\n\n{forward_entries}\n\n"
        else:
            new_doc += "# Ledger\n\n"
        new_doc += f"# Living State\n\n{living_state}"

        active_doc_path.write_text(new_doc, encoding="utf-8")

        # 6. Re-count and recompute
        comp_state.history_tokens = await self.adapter.count_tokens(new_doc)
        comp_state.compaction_number = 0

        # Adaptive headroom
        rotations_per_100 = (
            comp_state.archive_count
            / max(comp_state.global_compaction_number, 1)
        ) * 100

        if rotations_per_100 > 20:
            comp_state.conversation_headroom = int(
                comp_state.conversation_headroom * 0.95
            )

        comp_state.conversation_headroom = max(
            4000, min(comp_state.conversation_headroom, 40000)
        )

        comp_state.document_budget = compute_document_budget(
            MODEL_MAX_TOKENS,
            comp_state._system_overhead,
            comp_state.index_tokens,
            comp_state.conversation_headroom,
        )
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        # Emit compaction.rotation
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_ROTATION,
                    tenant_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "archive_count": comp_state.archive_count,
                        "new_document_budget": comp_state.document_budget,
                        "conversation_headroom": comp_state.conversation_headroom,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.rotation: %s", exc)

        logger.info(
            "Rotated compaction document for %s/%s — archive #%d",
            tenant_id, space_id, comp_state.archive_count,
        )
