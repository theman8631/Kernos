# SPEC-1B6: Tenant Isolation Verification + Test Suite

**Status:** READY FOR IMPLEMENTATION
**Depends on:** 1B.1–1B.5 (all complete)
**Objective:** Prove that two tenants on the same instance cannot access each other's data. This is a verification deliverable — no new features, just exhaustive proof that multi-tenancy isolation works across every data structure in the system. Also includes kernel integrity tests from the Phase 1B completion criteria.

**Why this matters:**
Multi-tenancy isolation was a foundational decision made at project start (Blueprint Part 3: "Every piece of state is keyed to a tenant_id from day one"). We've been building with this principle, but we've never systematically verified it. This spec closes that gap before we declare Phase 1B complete.

**What this produces:**
A comprehensive test suite (`tests/test_isolation.py`) that can be run on every future commit to ensure no change ever breaks tenant boundaries. Plus kernel integrity tests (`tests/test_kernel_integrity.py`) that verify the Phase 1B completion criteria.

-----

## Security Issue Found During Review

**`update_knowledge()` and `update_contract_rule()` in `state_json.py` scan ALL tenant directories** to find entries by ID. This means:

```python
async def update_knowledge(self, entry_id: str, updates: dict) -> None:
    for tenant_dir in self._data_dir.iterdir():  # <-- scans EVERY tenant
        if not tenant_dir.is_dir():
            continue
        path = tenant_dir / "state" / "knowledge.json"
        ...
```

If two tenants happen to have a knowledge entry with the same ID (unlikely with UUIDs but not impossible), or if a caller passes an entry_id that belongs to a different tenant, this function will find and modify another tenant's data. Same pattern exists in `update_contract_rule()`.

**Fix (include in this spec):** Both functions must accept `tenant_id` as a required parameter and scope their search to that tenant's directory only. Never scan across tenant boundaries.

```python
# Before (dangerous):
async def update_knowledge(self, entry_id: str, updates: dict) -> None:

# After (scoped):
async def update_knowledge(self, entry_id: str, tenant_id: str, updates: dict) -> None:
```

Update the `StateStore` abstract interface in `state.py` to match. Check all callers and update their signatures.

-----

## Component 1: Cross-Tenant Isolation Tests

**New file:** `tests/test_isolation.py`

Every test in this file creates two tenants (Tenant A and Tenant B) in the same data directory, writes data for Tenant A, and verifies Tenant B cannot see it. Uses `tmp_path` for clean isolation between tests.

### 1.1 Conversation Store Isolation

```python
# Tenant A stores a conversation. Tenant B queries the same conversation_id. Gets nothing.
# Tenant A stores a conversation. Tenant B lists conversations. Gets empty.
# Tenant A archives a conversation. Verify archive is in Tenant A's directory, not Tenant B's.
```

### 1.2 Event Stream Isolation

```python
# Tenant A emits events. Tenant B queries. Gets nothing.
# Tenant A emits events. Tenant B counts. Gets zero.
# Tenant A emits events with specific types. Tenant B queries those types. Gets nothing.
# Both tenants emit events. Each sees only their own. Verify counts match per-tenant.
```

### 1.3 State Store — Tenant Profile Isolation

```python
# Tenant A saves a profile. Tenant B queries. Gets None.
# Both tenants save profiles. Each retrieves only their own.
# Tenant A updates their profile. Tenant B's profile unchanged.
```

### 1.4 State Store — Soul Isolation

```python
# Tenant A hatches a soul. Tenant B queries. Gets None.
# Both tenants hatch souls with different names. Each retrieves only their own.
# Tenant A updates soul. Tenant B's soul unchanged.
```

### 1.5 State Store — Knowledge Isolation

```python
# Tenant A adds knowledge. Tenant B queries. Gets nothing.
# Tenant A adds knowledge about "John". Tenant B queries for "John". Gets nothing.
# Both tenants add knowledge about same subject. Each sees only their own.
# CRITICAL: update_knowledge with Tenant A's entry_id must NOT touch Tenant B's data.
```

### 1.6 State Store — Behavioral Contract Isolation

```python
# Tenant A has contracts. Tenant B queries. Gets nothing.
# Default contracts provisioned for Tenant A don't appear for Tenant B.
# CRITICAL: update_contract_rule with Tenant A's rule_id must NOT touch Tenant B's data.
```

### 1.7 State Store — Conversation Summary Isolation

```python
# Tenant A has conversation summaries. Tenant B lists. Gets nothing.
# Both tenants have conversations with same conversation_id. Each sees only their own.
```

### 1.8 Tenant Store Isolation

```python
# Tenant A provisioned. Tenant B queries Tenant A's ID. Behavior: creates new record (auto-provision), NOT returns Tenant A's data.
# Tenant A's directory structure doesn't contain Tenant B's files and vice versa.
```

### 1.9 Audit Store Isolation

```python
# Tenant A logs audit entries. Tenant B's audit directory is empty.
```

-----

## Component 2: Path Traversal / Adversarial Input Tests

**Same file:** `tests/test_isolation.py`

Test that malicious or malformed tenant IDs cannot escape their sandbox.

### 2.1 Path Traversal Attempts

```python
# tenant_id = "../../etc/passwd" — _safe_name must neutralize this
# tenant_id = "../other_tenant/state/soul" — must not reach other tenant's files
# tenant_id = "tenant_a/../../tenant_b" — must not escape
# Verify _safe_name handles: "..", "/", "\", ":", and empty strings
```

### 2.2 Malformed Input

```python
# Empty tenant_id — should not crash, should create a safe directory name
# Very long tenant_id (1000+ chars) — should not crash
# tenant_id with unicode — should handle gracefully
# tenant_id with null bytes — should not crash
# conversation_id with path separators — should be sanitized
```

### 2.3 _safe_name Coverage

```python
# Verify _safe_name strips or replaces all dangerous characters
# Verify _safe_name is used consistently — every path construction uses it
# NOTE: Current _safe_name only handles ":", "/", "\" — it does NOT handle ".."
# If ".." is not handled, add it. This is a real vulnerability.
```

**Important:** If `_safe_name` does not currently handle `..` (path traversal), this spec requires adding that handling. A tenant_id of `../other_tenant` would currently resolve to a valid path outside the tenant's sandbox. This is a must-fix.

-----

## Component 3: Kernel Integrity Tests

**New file:** `tests/test_kernel_integrity.py`

These verify the Phase 1B completion criteria that aren't covered by isolation.

### 3.1 Restart Survival

```python
# Create a full tenant state: soul, profile, contracts, knowledge, events, conversations.
# "Restart" by creating new store instances pointing at the same data_dir.
# Verify all state loads correctly from disk.
# Verify soul.hatched is True, interaction_count is correct.
# Verify events are queryable after restart.
# Verify conversation history loads correctly after restart.
```

### 3.2 Event Completeness

```python
# Simulate a full message flow (using handler with mock reasoning).
# Verify these events are emitted in order:
#   message.received → reasoning.request → reasoning.response → task.completed → message.sent
# Verify each event has: id, type, tenant_id, timestamp, source, payload.
# Verify reasoning events include: model, tokens, cost, duration.
```

### 3.3 Cost Tracking

```python
# Process multiple messages through the task engine (with mock reasoning returning known token counts).
# Verify each task.completed event has accurate: input_tokens, output_tokens, estimated_cost_usd, duration_ms.
# Verify cost calculation matches MODEL_PRICING for known models.
# Verify unknown model returns 0.0 cost (not crash).
```

### 3.4 Shadow Archive

```python
# Create a conversation. Archive it.
# Verify: original file removed from conversations directory.
# Verify: archive copy exists in archive/conversations/{timestamp}/ with full metadata.
# Verify: archive copy includes archived_at timestamp and original conversation data.
# This is the non-destructive deletion principle in action.
```

### 3.5 Behavioral Contract Defaults

```python
# Provision a new tenant.
# Verify 7 default contract rules are created.
# Verify rule types: 2 must_not, 2 must, 1 preference, 1 escalation (+ 1 more must_not).
# Verify all rules are active, source is "default", tenant_id matches.
```

-----

## Component 4: Fix update_knowledge and update_contract_rule

**Modified file:** `kernos/kernel/state.py`

Update abstract interface:

```python
# Before:
@abstractmethod
async def update_knowledge(self, entry_id: str, updates: dict) -> None: ...

@abstractmethod
async def update_contract_rule(self, rule_id: str, updates: dict) -> None: ...

# After:
@abstractmethod
async def update_knowledge(self, tenant_id: str, entry_id: str, updates: dict) -> None: ...

@abstractmethod
async def update_contract_rule(self, tenant_id: str, rule_id: str, updates: dict) -> None: ...
```

**Modified file:** `kernos/kernel/state_json.py`

Scope both functions to the specified tenant directory only:

```python
async def update_knowledge(self, tenant_id: str, entry_id: str, updates: dict) -> None:
    path = self._state_dir(tenant_id) / "knowledge.json"
    if not path.exists():
        logger.warning("update_knowledge: no knowledge file for tenant %s", tenant_id)
        return
    raw = self._read_json(path, [])
    for i, d in enumerate(raw):
        if d.get("id") == entry_id:
            raw[i].update(updates)
            self._write_json(path, raw)
            return
    logger.warning("update_knowledge: entry_id %s not found for tenant %s", entry_id, tenant_id)
```

Same pattern for `update_contract_rule`. Check all callers and update signatures.

-----

## Component 5: _safe_name Hardening

**Modified files:** `kernos/persistence/json_file.py`, `kernos/kernel/events.py`, `kernos/kernel/state_json.py`

All three files have their own `_safe_name` function. Consolidate into one shared utility and add path traversal protection:

```python
def _safe_name(s: str) -> str:
    """Convert a string to a safe filesystem name.

    Prevents path traversal and neutralizes dangerous characters.
    """
    # Remove path traversal
    s = s.replace("..", "")
    # Replace path separators and other dangerous chars
    s = s.replace("/", "_").replace("\\", "_").replace(":", "_")
    # Remove null bytes
    s = s.replace("\x00", "")
    # Ensure non-empty
    if not s or not s.strip():
        s = "_empty_"
    return s
```

Move to a shared location (e.g., `kernos/utils.py` or `kernos/persistence/utils.py`) and import everywhere. The three duplicate implementations should be replaced with one.

-----

## Implementation Order

1. **Fix _safe_name** — consolidate and harden (path traversal protection)
2. **Fix update_knowledge / update_contract_rule** — add tenant_id scoping, update interface and all callers
3. **Write isolation tests** (Component 1) — cross-tenant for every data structure
4. **Write adversarial tests** (Component 2) — path traversal, malformed input
5. **Write kernel integrity tests** (Component 3) — restart, events, cost, archive, contracts
6. **Run full test suite** — everything passes

-----

## What Claude Code MUST NOT Change

- Any existing data model structure (Soul, TenantProfile, ContractRule, etc.)
- Handler message flow
- Event emission patterns
- Template content (just refined in 1B.5)
- CLI behavior (just fixed in 1B.5)

The ONLY code changes are: _safe_name hardening, update_knowledge/update_contract_rule scoping, and new test files. This is a verification spec, not a feature spec.

-----

## Acceptance Criteria

1. **All isolation tests pass.** Two tenants on the same instance, exhaustively verified across every data structure: conversations, events, state (profile, soul, knowledge, contracts, summaries), tenant records, audit logs.

2. **Path traversal blocked.** `_safe_name("../../etc/passwd")` produces a safe directory name. `_safe_name("../other_tenant")` produces a safe directory name. No path construction in the codebase can escape the tenant sandbox.

3. **update_knowledge and update_contract_rule are tenant-scoped.** Neither function scans across tenant directories. Both require tenant_id. Tests prove Tenant A's entry_id cannot be used to modify Tenant B's data.

4. **Kernel survives restart.** All state loads correctly from disk after new store instances are created. Soul, profile, contracts, events, conversations all persist.

5. **Events are complete.** Full message flow produces the expected event chain with all required fields including model, tokens, cost, duration.

6. **Shadow archive works.** Archiving relocates, never destroys. Archive copy has full metadata.

7. **Zero regressions.** All existing tests still pass. New tests add coverage without breaking anything.

-----

## Live Verification

Live verification: **N/A.** This is a test infrastructure deliverable. The tests themselves are the verification. The founder can review test output and inspect the _safe_name fix, but there's no user-facing change to test on Discord.

**Founder review:** After Claude Code completes, run `python -m pytest tests/ -v` and verify the new test files appear and all pass. Optionally inspect the _safe_name consolidation and the update_knowledge/update_contract_rule signature changes to confirm they look right.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Tenant-scoped updates | update_knowledge/update_contract_rule require tenant_id | Cross-tenant scanning is a security vulnerability. Always scope to one tenant. |
| Shared _safe_name | One utility, imported everywhere | Three duplicate implementations with inconsistent handling is a maintenance and security risk. |
| Path traversal in _safe_name | Strip ".." along with other dangerous chars | tenant_id comes from user-controlled input (platform:sender). Must be treated as untrusted. |
| Tests as the deliverable | No user-facing changes, test suite is the output | Isolation is proven by tests, not by features. The test suite runs on every future commit. |
