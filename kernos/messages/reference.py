"""Kernos self-knowledge reference document.

Written to the system space at tenant provisioning. The agent uses this
to answer questions about how Kernos works. Updated as part of every
spec's post-implementation checklist.
"""

KERNOS_REFERENCE = """
# Kernos Architecture Reference

Quick reference for how the system works. Each section covers one component:
what it does, how it works, and where the code lives. Use read_source(path)
to inspect implementation details.

---

## Context Spaces

What: Every conversation is routed to a domain-specific context space (daily,
project, system, etc.). The agent operates in a per-space thread — not a flat
history. Cross-domain context from other spaces is injected as background signals.

How: An LLM router (one Haiku call per message) assigns each message to the
right space. New spaces are auto-created via a two-gate process: Gate 1
accumulates topic hints over 15 messages, Gate 2 asks a model whether a new
space is warranted. Each space has a posture (working style), active tools,
and compaction state. Spaces are LRU-archived at a cap of 40.

Code: `kernel/spaces.py` (ContextSpace dataclass), `kernel/router.py`
(LLMRouter), `messages/handler.py` (routing, Gate 1/2, space switching)

---

## Memory & Knowledge

What: Kernos extracts and persists knowledge from conversations. Facts, entities,
preferences, and behavioral instructions are stored as KnowledgeEntry records with
lifecycle tracking, foresight signals, and retrieval strength decay.

How: Two-stage extraction runs after every response. Tier 1 (synchronous, zero cost)
pattern-matches for name and communication style. Tier 2 (async LLM, ~$0.004/msg)
extracts structured facts via a schema, resolves entities, and deduplicates against
existing knowledge. Each entry has a lifecycle archetype (identity/structural/habitual/
contextual/ephemeral) controlling decay rate, and optional foresight signals for
proactive awareness.

Code: `kernel/projectors/coordinator.py` (entry point), `kernel/projectors/rules.py`
(Tier 1), `kernel/projectors/llm_extractor.py` (Tier 2), `kernel/state.py`
(KnowledgeEntry, compute_retrieval_strength)

---

## Entities

What: Named mentions (people, places, organizations) are resolved to canonical
EntityNode records. The system prevents duplicates while respecting ambiguity —
if "Alex" could be two different people, both records are kept with a MAYBE_SAME_AS
edge rather than force-merging.

How: Three-tier resolution cascade. Tier 1 (deterministic): exact name/alias/contact
match — resolves 80%+ of cases. Tier 2 (scoring): Jaro-Winkler + phonetic + embedding
similarity + token overlap — scores above 0.85 match. Tier 3 (LLM): structured output
call for ambiguous cases (0.50-0.85 range). Fact deduplication uses cosine similarity
in three zones: >0.92 reinforce, 0.65-0.92 LLM classify, <0.65 add new.

Code: `kernel/entities.py` (EntityNode, IdentityEdge), `kernel/resolution.py`
(EntityResolver), `kernel/dedup.py` (FactDeduplicator), `kernel/embeddings.py`
(EmbeddingService)

---

## Compaction

What: Replaces naive message truncation with structured history preservation. Each
space maintains a two-layer document: an append-only Ledger of immutable historical
entries and a rewritable Living State snapshot of current truth.

How: After every exchange, tokens are tracked. When accumulated new tokens exceed
the ceiling, one Haiku call compacts unprocessed messages into a new Ledger entry
and rewrites the Living State. When the document grows too large, it's sealed as
an archive and a summary index generated. The system maintains a file manifest in
the Living State so the agent knows what files exist across compaction boundaries.

Code: `kernel/compaction.py` (CompactionState, CompactionService), `kernel/tokens.py`
(token counting), `messages/handler.py` (_assemble_space_context)

---

## Covenant Rules

What: Behavioral contracts that guide agent actions. Every tenant starts with seven
default rules (confirm before spending, don't send messages without approval, etc.).
Users add rules through natural conversation — "never email my ex" becomes a
structured must_not rule. **Rules are automatically captured by the kernel** from
behavioral instructions — the agent does NOT need to create them.

How: The NL Contract Parser in Tier 2 extraction (the sole creation path) detects
instructions and creates CovenantRule records. After each write, an LLM validation
call checks the full set for duplicates (MERGE), contradictions (CONFLICT — surfaces
as a whisper for user resolution), and poorly worded rules (REWRITE). The dispatch
gate enforces must_not rules before tool execution. Use manage_covenants to view or
edit existing rules — not to create new ones.

Code: `kernel/contract_parser.py` (parse_behavioral_instruction), `kernel/state.py`
(CovenantRule), `kernel/covenant_manager.py` (validate_covenant_set, manage_covenants),
`messages/handler.py` (rule loading, system prompt injection)

---

## Dispatch Gate

What: Gates write/action tool calls before execution. Reads pass silently. Writes
go through three-step authorization with no keyword matching — the model is the
sole correctness authority.

How: Step 1 (token check): programmatic approval tokens for API callers. Step 2
(permission override): fast dict lookup on tenant profile — "always-allow" bypasses
the gate entirely. Step 3 (model evaluation): one cheap Haiku call sees recent user
turns, agent reasoning, tool details, and covenant rules, then returns EXPLICIT
(user asked for it), AUTHORIZED (standing rule covers it), CONFLICT (user asked but
must_not applies), or DENIED (no authorization). Blocked actions become PendingActions
the user can confirm via [CONFIRM:N].

Code: `kernel/reasoning.py` (_gate_tool_call, _evaluate_gate, GateResult,
PendingAction), `messages/handler.py` (confirmation replay)

---

## Proactive Awareness

What: Makes Kernos proactive — surfacing time-sensitive signals at conversation
start without the user asking. The system notices upcoming deadlines and appointments
from the knowledge store and mentions them at the next natural moment.

How: The AwarenessEvaluator runs on a periodic timer (default 30 min). Its time pass
queries knowledge entries with foresight_expires in the next 48 hours, packages them
as Whisper objects (stage <12h, ambient 12-48h), and queues them. At session start,
pending whispers are injected into the system prompt. A suppression registry prevents
nagging — once surfaced, the same signal won't repeat unless the underlying knowledge
changes. The dismiss_whisper tool lets users suppress specific insights.

Code: `kernel/awareness.py` (AwarenessEvaluator, Whisper, SuppressionEntry),
`messages/handler.py` (_get_pending_awareness)

---

## Files

What: Per-space persistent file storage. The agent can create, read, list, and
delete text files scoped to the active context space. Files are never permanently
destroyed — deletes move to a shadow archive.

How: Four kernel tools (write_file, read_file, list_files, delete_file) operate
through FileService. Each space has its own files directory with a .manifest.json
tracking metadata. write_file and delete_file are gated by the dispatch interceptor.
The manifest is injected into compaction's Living State so file awareness persists
across compaction boundaries.

Code: `kernel/files.py` (FileService, FILE_TOOLS)

---

## MCP Tools & Capabilities

What: External tool integration via the Model Context Protocol. Capabilities
(calendar, email, web browser) are MCP servers that the system connects to,
discovers tools from, and routes tool calls through.

How: The CapabilityRegistry tracks known capabilities and their connection status.
At startup, registered MCP servers connect and their tools are discovered. Tools
are scoped per space — each space has an active_tools list plus universal capabilities.
The request_tool meta-tool lets the agent activate capabilities for the current space.
Secure credential handoff intercepts the next user message as a secret, writes it
to disk (never entering the LLM context), and uses it to connect the capability.

Code: `capability/registry.py` (CapabilityRegistry, CapabilityInfo),
`capability/known.py` (KNOWN_CAPABILITIES catalog), `capability/client.py`
(MCPClientManager), `messages/handler.py` (credential flow, config persistence)

---

## Web Browser

What: Your way to find current information on the web. When the user asks you to
search for something, look something up, or find current information — use this.
Navigate to a relevant site or search engine (e.g. google.com), read the page with
the markdown tool, and answer the question. You can read any page on the internet.

How: Seven tools: goto (navigate to URL), markdown (get page content — accepts an
optional URL so you can navigate and read in one call), links (extract all links),
semantic_tree (DOM for AI reasoning), interactiveElements (forms/buttons),
structuredData (JSON-LD/OpenGraph), evaluate (run JS). All tools except evaluate
bypass the dispatch gate.

Code: `capability/known.py` (web-browser entry). Binary: ~/bin/lightpanda
(x86_64 Linux only).

---

## Event Stream

What: Append-only, immutable log of everything that happens. The kernel's audit
trail. Events are never modified after writing. This is NOT the runtime query
surface — that's the State Store.

How: Events are partitioned by tenant and date into daily JSON files. Each event
has a time-sortable ID, hierarchical type string, tenant_id, timestamp, source,
and payload. Event types cover message lifecycle, reasoning, tool calls, knowledge
extraction, entity resolution, space changes, compaction, covenant rules, dispatch
gate decisions, and proactive insights.

Code: `kernel/events.py` (Event, EventStream, JsonEventStream, emit_event),
`kernel/event_types.py` (EventType enum)

---

## Retrieval (Remember Tool)

What: Semantic memory search across knowledge entries, entity graph, and compaction
archives. The agent calls remember(query) to look up what it knows.

How: Three-stage pipeline. Stage 1 gathers candidates concurrently: semantic search
over KnowledgeEntries (embedding similarity), entity name/alias matching with
SAME_AS resolution, and compaction archive search (two Haiku calls: index match +
extraction). Stage 2 ranks by quality score (recency 0.4 + confidence 0.3 +
reinforcement 0.3) with space relevance and foresight boosts. Stage 3 formats
results with a hard cap of 1500 tokens.

Code: `kernel/retrieval.py` (RetrievalService, REMEMBER_TOOL)

---

## Source Introspection (read_source)

What: Lets the agent read its own source code when users ask how something works
technically or want to see implementation details.

How: The read_source kernel tool takes a relative path within the kernos/ package
(e.g., "kernel/awareness.py") and an optional section name (class or function).
Security: rejects absolute paths, path traversal (..), and paths outside kernos/.
Read-effect — no dispatch gate.

Code: `kernel/reasoning.py` (READ_SOURCE_TOOL, _read_source)

---

## Covenant Management (manage_covenants)

What: Lets users view, edit, and remove their standing behavioral rules.
Post-write LLM validation auto-cleans duplicates and surfaces contradictions.

How: The manage_covenants kernel tool supports three actions: list (show active
rules grouped by type), remove (soft-remove), and update (create new rule,
supersede old — audit trail preserved). After every rule write, validate_covenant_set()
fires a Haiku call checking the full set for MERGE (auto-resolve duplicates),
CONFLICT (create whisper for user), and REWRITE (auto-improve wording). A startup
migration handles exact/near-exact duplicates at zero LLM cost.

Code: `kernel/covenant_manager.py` (validate_covenant_set, manage_covenants tool,
cleanup migration), `kernel/projectors/coordinator.py` (creation + validation trigger)

---

## Kernel Tools Summary

Always available in every space:

| Tool | Effect | What it does |
|------|--------|-------------|
| remember | read | Search memory (knowledge, entities, archives) |
| manage_covenants | soft_write | View, update, or remove behavioral rules |
| write_file | soft_write | Create/update text file in active space |
| read_file | read | Read file from active space |
| list_files | read | List files and descriptions in active space |
| delete_file | soft_write | Soft-delete file (moves to shadow archive) |
| request_tool | read | Activate an MCP capability for current space |
| dismiss_whisper | read | Suppress a proactive awareness insight |
| read_source | read | Read Kernos source code |

MCP tools vary by space — check active capabilities with list_files in System space
or ask "what tools do I have?"
"""
