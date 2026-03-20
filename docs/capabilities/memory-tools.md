# Memory Tools

The agent has tools for actively searching and managing its memory.

## remember (Kernel Tool)

Search across all knowledge sources. Use this when you need to recall something about the user, a person they mentioned, a past conversation, or any stored fact.

| Field | Value |
|-------|-------|
| Effect | read (no gate) |
| Input | `query` — natural language search query |
| Output | Ranked results from knowledge entries, entity graph, and compaction archives |

### How It Works

Three-stage concurrent pipeline:

1. **Knowledge entries** — semantic search via embedding similarity across all `KnowledgeEntry` records
2. **Entity graph** — name/alias matching with `SAME_AS` edge resolution for canonical entities
3. **Compaction archives** — two Haiku calls (index match to find relevant archive, then extraction from that archive)

Results are ranked by: recency (0.4) + confidence (0.3) + reinforcement (0.3), with boosts for space relevance and active foresight signals. Hard cap of 1500 tokens.

### When to Use

- User asks "do you remember...?" or "what do you know about...?"
- You need context about a person, project, or past discussion
- You want to check if you already know something before asking the user

## Automatic Memory

You do not need to call `remember` to store knowledge. Memory extraction happens automatically after every response:

- **Tier 1** (zero cost) — pattern-matches user name and communication style
- **Tier 2** (LLM, ~$0.004/msg) — extracts structured facts, resolves entities, deduplicates

The user should never have to say "remember this" — the system captures it automatically.

## Code Locations

| Component | Path |
|-----------|------|
| REMEMBER_TOOL, RetrievalService | `kernos/kernel/retrieval.py` |
| Extraction coordinator | `kernos/kernel/projectors/coordinator.py` |
