# SPEC: Conversation Logs Phase 4 — Deep Recall with remember_details()

**Status:** APPROVED — Kit reviewed, all eight points incorporated.  
**Author:** Architect  
**Date:** 2026-03-24  
**Depends on:** P1 (SHIPPED), P2 (SHIPPED), P3 (compaction from logs)  
**Phase:** 4 of 4  
**Type:** New read-only kernel tool for exact-source retrieval from archived logs.

---

## Objective

Add `remember_details(source_ref)` as a read-only kernel tool that retrieves 
the exact conversation text from a specific archived log file. This completes 
the two-tier recall system.

**This tool does exactly ONE thing:** given a specific source log reference, 
retrieve exact text from that archived log, optionally narrowed by query.

**This tool does NOT:**
- Search across all archived logs
- Replace remember()
- Mutate any state, logs, or compaction data

**Intended usage pattern (normative, not optional):**
1. `remember(query)` — finds the memory + Ledger entry with `source: log_NNN`
2. `remember_details(source_ref)` — opens that specific source log

If the agent doesn't have a source ref, it should call `remember()` first. 
Not `remember_details()` with a guess.

---

## Two-Tier Recall

**Tier 1 — `remember(query)`:** Searches Ledger + Living State + knowledge 
entries. Returns editorial summaries including `source: log_NNN` references. 
Good enough 90% of the time.

**Tier 2 — `remember_details(source_ref, query?)`:** Opens a specific archived 
log and returns the exact conversation text. Use when the summary isn't enough 
and the user needs the actual words.

### The Two-Step UX

```
User: "What was the Henderson thing about?"
Agent: [calls remember("Henderson")]
Agent: "Back in early March, you discussed the Henderson deal and decided 
       to wait until Q2. (source: log_003)"

User: "Did I say to wait or to push? I can't remember the exact reasoning."
Agent: [calls remember_details("log_003", query="Henderson")]
Agent: "Here's what you said: 'I think Henderson is bluffing but I don't 
       want to push until we see Q2 numbers. If Q2 looks strong, we push. 
       If not, we walk.'"
```

---

## Implementation

### Component 1: Public logger API for archived log access

Add `read_log_text()` to ConversationLogger as a public method. The handler 
NEVER constructs log paths directly.

```python
async def read_log_text(
    self, tenant_id: str, space_id: str, log_number: int
) -> str | None:
    """Read the full text of an archived or current log file.
    
    Returns the log text, or None if the file doesn't exist.
    Public API — used by remember_details handler.
    """
    log_path = self._logs_dir(tenant_id, space_id) / f"log_{log_number:03d}.txt"
    if not log_path.exists():
        return None
    return log_path.read_text(encoding="utf-8")
```

### Component 2: remember_details kernel tool

Add to `_KERNEL_TOOLS` and `_KERNEL_READS` (read-only, no state mutation).

**Tool definition:**

```python
{
    "name": "remember_details",
    "description": (
        "Retrieve exact conversation text from a specific archived source log. "
        "Use after remember() when a Ledger entry includes 'source: log_NNN'. "
        "Optional query narrows to the relevant section within that log. "
        "This is a read-only operation — no state is changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_ref": {
                "type": "string",
                "description": (
                    "The log reference to retrieve, e.g., 'log_003'. "
                    "Get this from a Ledger entry returned by remember()."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional keyword to find the relevant section within "
                    "the log. Returns matching lines with surrounding context. "
                    "If omitted, returns the full log (bounded)."
                ),
            },
        },
        "required": ["source_ref"],
    },
}
```

### Component 3: Handler implementation

```python
async def _handle_remember_details(
    self, tenant_id: str, space_id: str, input_data: dict
) -> str:
    """Retrieve conversation text from a specific archived log file.
    
    Read-only. No state mutation. No log mutation. No compaction changes.
    """
    source_ref = input_data.get("source_ref", "")
    query = input_data.get("query", "")
    
    if not source_ref:
        return (
            "No source reference provided. Call remember() first to find "
            "a Ledger entry with a source log reference (e.g., 'source: log_003'), "
            "then pass that reference here."
        )
    
    # Parse log reference — accept "log_003", "log_3", "3", "log003"
    log_number = self._parse_log_ref(source_ref)
    
    if log_number is None:
        return (
            f"Could not parse '{source_ref}' as a log reference. "
            f"Expected format: 'log_003' or '3'. "
            f"Call remember() first to find the correct source reference."
        )
    
    # Read via public ConversationLogger API
    log_text = await self.conv_logger.read_log_text(
        tenant_id, space_id, log_number
    )
    
    if log_text is None:
        logger.info(
            "DEEP_RECALL: space=%s log=%03d not_found", space_id, log_number
        )
        return f"Log file log_{log_number:03d} not found for this space."
    
    # If a query is provided, extract relevant section
    if query:
        relevant = self._extract_relevant_section(log_text, query)
        if relevant:
            logger.info(
                "DEEP_RECALL: space=%s log=%03d query=%s chars=%d",
                space_id, log_number, query[:50], len(relevant),
            )
            return (
                f"From log_{log_number:03d} — section matching '{query}':"
                f"\n\n{relevant}"
            )
        else:
            return (
                f"Log_{log_number:03d} exists but no section matches '{query}'. "
                f"Try a different search term, or omit the query to see the full log."
            )
    
    # No query — return bounded log content
    max_chars = 8000  # ~2000 tokens
    
    if len(log_text) <= max_chars:
        logger.info(
            "DEEP_RECALL: space=%s log=%03d full chars=%d",
            space_id, log_number, len(log_text),
        )
        return f"From log_{log_number:03d} (full log):\n\n{log_text}"
    
    # Log too large — return first chunk + last chunk with notice
    chunk_size = max_chars // 2
    head = log_text[:chunk_size]
    tail = log_text[-chunk_size:]
    
    logger.info(
        "DEEP_RECALL: space=%s log=%03d bounded chars=%d (total=%d)",
        space_id, log_number, max_chars, len(log_text),
    )
    return (
        f"From log_{log_number:03d} ({len(log_text)} chars total, "
        f"showing first and last sections):\n\n"
        f"--- START ---\n{head}\n\n"
        f"--- GAP ({len(log_text) - max_chars} chars omitted) ---\n\n"
        f"--- END ---\n{tail}\n\n"
        f"To see a specific section, retry with a query keyword."
    )

def _parse_log_ref(self, ref: str) -> int | None:
    """Parse a log reference string into a log number.
    
    Accepts: "log_003", "log_3", "3", "log003"
    Returns: 3 (or None if unparseable)
    """
    import re
    match = re.match(r'log_?(\d+)', ref.strip().lower())
    if match:
        return int(match.group(1))
    try:
        return int(ref.strip())
    except ValueError:
        return None

def _extract_relevant_section(
    self, log_text: str, query: str, context_lines: int = 10
) -> str:
    """Extract lines from a log relevant to a query.
    
    Simple keyword matching with surrounding context lines.
    Good enough for v1 — within a single known log.
    """
    lines = log_text.split("\n")
    query_lower = query.lower()
    
    matching_indices = []
    for i, line in enumerate(lines):
        if query_lower in line.lower():
            matching_indices.append(i)
    
    if not matching_indices:
        return ""
    
    included = set()
    for idx in matching_indices:
        start = max(0, idx - context_lines)
        end = min(len(lines), idx + context_lines + 1)
        for i in range(start, end):
            included.add(i)
    
    return "\n".join(lines[i] for i in sorted(included))
```

---

## What NOT to Change

- **remember()** — unchanged. Returns Ledger entries that contain `source: log_NNN` 
  from P3. The agent reads the source ref naturally from the Ledger text.
- **Compaction** — unchanged from P3.
- **Conversation logs** — P1 writing and P2 reading unchanged.
- **ConversationLogger internals** — handler uses only `read_log_text()` public method.
- **No state mutation of any kind** — this is a pure read tool.

---

## Logging

| Log line | When |
|----------|------|
| `DEEP_RECALL: space={id} log={num} query={q} chars={n}` | Relevant section retrieved |
| `DEEP_RECALL: space={id} log={num} full chars={n}` | Full log retrieved (small enough) |
| `DEEP_RECALL: space={id} log={num} bounded chars={n} (total={t})` | Log truncated (head+tail) |
| `DEEP_RECALL: space={id} log={num} not_found` | Requested log doesn't exist |

---

## Acceptance Criteria

1. `remember_details("log_003")` retrieves text from archived `log_003.txt`
2. `remember_details("log_003", query="Henderson")` returns relevant excerpt 
   with surrounding context lines
3. Invalid or missing source refs return helpful error message pointing to 
   `remember()` as the first step
4. Large logs without query return head + tail with gap notice and suggestion 
   to retry with query
5. Small logs without query return full text
6. `remember_details` is in `_KERNEL_TOOLS` and `_KERNEL_READS` (read-only)
7. Handler uses only `conv_logger.read_log_text()` — no internal path construction
8. Intended UX works: `remember()` → source ref in Ledger → `remember_details(ref)`
9. `DEEP_RECALL` events logged at INFO
10. No changes to `remember()`, compaction, or log writing
11. No state mutation of any kind
12. All existing tests pass

---

## Live Test

1. Ensure P3 has run at least once (Ledger contains `source: log_001`)
2. Ask: "What was [topic] about?" — agent calls `remember()`, gets summary with source ref
3. Ask: "What exactly did I say?" — agent calls `remember_details("log_001", query="[topic]")`
4. Verify: response contains actual conversation text from the log, not paraphrase
5. Test without query on a small log — full text returned
6. Test without query on a large log — head + tail + gap notice
7. Test with invalid ref — helpful error pointing to `remember()`
8. Test with valid ref but no matching query — clear "no match" message
9. Regression: all existing functionality works

---

## Design Decisions

| Decision | Choice | Why | Who |
|----------|--------|-----|-----|
| One job only | Retrieve from specific log ref, no cross-log search | Cleaner mental model, cheaper, predictable | Kit |
| Public logger API | `read_log_text()` on ConversationLogger | No internal path access from handler | Kit |
| Tool description | "Use after remember()..." | Guides normative two-step pattern | Kit |
| Within-log extraction | Keyword matching + context lines | Good enough for v1, simple | Kit |
| No archive-wide search | Cut from v1 | Expensive, false positives, unpredictable | Kit |
| Truncation policy | Head + tail + gap notice | Better than tail-only for earlier content | Kit |
| Read-only | Explicitly stated, no mutations | Clean tool boundary | Kit |
| Two-step UX | Normative, not optional | Prevents overuse of details when summary suffices | Kit |
| Flexible ref parsing | "log_003", "3", "log_3" all work | Agent doesn't need to be precise | Architect |
