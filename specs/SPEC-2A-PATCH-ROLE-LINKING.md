# SPEC-2A-PATCH: Relationship-Role Entity Linking

**Status:** READY FOR IMPLEMENTATION
**Depends on:** SPEC-2A (complete)
**Objective:** Fix the split-entity problem where "my wife" and "Liana" create two separate EntityNodes for the same person. The fix is at the source: extraction and resolution.

**The problem in live data:**
"My wife loves cooking" → EntityNode `canonical_name: "user's wife", relationship_type: "wife"`
"Liana gave me cookies" → EntityNode `canonical_name: "Liana"` (separate, no connection)
"My wife Liana said..." → Should connect them. Currently doesn't.

**Why this blocks 2C:** The `remember` tool returns incomplete results when entities are split. "What do you know about my wife?" returns half the facts. "What do you know about Liana?" returns the other half. The user experiences this as the agent having a bad memory.

-----

## Change 1: Extraction Prompt Update

**Modified file:** `kernos/kernel/projectors/llm_extractor.py`

Add to the entity extraction instructions in the Tier 2 prompt:

```
When a name and a relationship role appear together in the same phrase
('my wife Liana', 'Liana, my wife', 'my boss Tom', 'Tom, who is my boss'),
emit ONE entity with both the name and the relationship type.
Do NOT emit the role and the name as separate entities.

Example:
  "My wife Liana loves cooking"
  → entity: {name: "Liana", type: "person", relationship_type: "wife"}
  NOT: {name: "user's wife"} AND {name: "Liana"} as two entities

When only a role is mentioned without a name ('my wife called'),
emit the role-based entity as before: {name: "user's wife", relationship_type: "wife"}

When only a name is mentioned without a role ('Liana called'),
emit the name-based entity as before: {name: "Liana"}
```

This handles the forward case — new messages with name+role together produce one clean entity.

-----

## Change 2: Relationship-Role Resolution in Tier 1

**Modified file:** `kernos/kernel/resolution.py`

Add a new check to Tier 1 resolution: when the incoming entity has a `relationship_type`, search for an existing entity whose `canonical_name` is the role form OR whose `relationship_type` matches.

```python
# In _tier1_resolve(), after alias matching and before contact info matching:

# Check 3: Relationship-role match
# "Liana" with relationship_type="wife" should match existing "user's wife"
if incoming_relationship_type:
    role_forms = [
        f"user's {incoming_relationship_type}",
        f"my {incoming_relationship_type}",
        incoming_relationship_type,
    ]
    for node in existing:
        # Match by role in canonical name
        if node.canonical_name.lower() in [r.lower() for r in role_forms]:
            return node, "role_match"
        # Match by relationship_type field
        if node.relationship_type and node.relationship_type.lower() == incoming_relationship_type.lower():
            # Only match if the existing node doesn't already have a real name
            # that differs from the incoming name
            if node.canonical_name.lower() in [r.lower() for r in role_forms]:
                return node, "role_match"
            # Existing node has a real name — check if it's the same person
            # (e.g., existing "Liana" with relationship_type="wife")
            # This is already handled by name matching above
            pass
```

### On role_match: update the entity

When a role_match is found, the existing entity gets upgraded with the real name:

```python
# In the resolution outcome handler:
if resolution_type == "role_match":
    # Upgrade: real name becomes canonical, role becomes alias
    old_name = matched_node.canonical_name  # e.g., "user's wife"
    matched_node.canonical_name = incoming_name  # e.g., "Liana"
    if old_name not in matched_node.aliases:
        matched_node.aliases.append(old_name)
    if incoming_name not in matched_node.aliases:
        matched_node.aliases.append(incoming_name)
    if incoming_relationship_type and not matched_node.relationship_type:
        matched_node.relationship_type = incoming_relationship_type
    await self.state.save_entity_node(matched_node)
```

After this: `canonical_name: "Liana", aliases: ["user's wife", "Liana"], relationship_type: "wife"`. All previously linked knowledge entries follow because they reference the EntityNode by ID.

-----

## Change 3: Reconcile Existing Split Data

For tenants that already have split entities (like the live test data), the resolution needs to handle the case where BOTH a role-based entity AND a separate named entity exist.

When Tier 1 finds a role_match AND there's also an existing entity with the same name but no relationship_type, merge them:

```python
# After role_match resolution:
# Check if there's ALSO a separate named entity that should be merged
if resolution_type == "role_match":
    # Look for an existing entity with the incoming name but no role
    for other_node in existing:
        if (other_node.id != matched_node.id and
            other_node.canonical_name.lower() == incoming_name.lower() and
            other_node.entity_type == matched_node.entity_type):
            # Merge: move other_node's knowledge entries to matched_node
            for entry_id in other_node.knowledge_entry_ids:
                if entry_id not in matched_node.knowledge_entry_ids:
                    matched_node.knowledge_entry_ids.append(entry_id)
                # Update the KnowledgeEntry's entity_node_id
                entry = await self.state.get_knowledge_entry(tenant_id, entry_id)
                if entry:
                    entry.entity_node_id = matched_node.id
                    await self.state.save_knowledge_entry(entry)
            # Add other_node's aliases
            for alias in other_node.aliases:
                if alias not in matched_node.aliases:
                    matched_node.aliases.append(alias)
            # Deactivate the duplicate
            other_node.active = False
            await self.state.save_entity_node(other_node)
            # Create SAME_AS edge for audit
            await self.state.save_identity_edge(IdentityEdge(
                source_id=matched_node.id,
                target_id=other_node.id,
                edge_type="SAME_AS",
                confidence=1.0,
                evidence_signals=["role_name_merge"],
                created_at=_now_iso(),
            ))
            await self.state.save_entity_node(matched_node)
```

This reconciliation runs naturally the next time the user mentions "my wife Liana" — no background job needed. The split heals itself on the next relevant message.

-----

## Implementation Order

1. Extraction prompt update (Change 1)
2. Tier 1 relationship-role matching (Change 2)
3. Entity upgrade on role_match (Change 2)
4. Split entity reconciliation (Change 3)
5. Tests — role matching, entity upgrade, split reconciliation, existing data compatibility

-----

## Acceptance Criteria

1. **Name+role together produces one entity.** "My wife Liana loves cooking" → one EntityNode: `canonical_name: "Liana", relationship_type: "wife"`. NOT two entities. Verified via `kernos-cli entities`.

2. **Role-only still works.** "My wife called" → EntityNode `canonical_name: "user's wife", relationship_type: "wife"` (same as before). Verified.

3. **Name-only still works.** "Liana called" → resolves to existing Liana entity if one exists, creates new if not (same as before). Verified.

4. **Role match upgrades the entity.** Existing "user's wife" entity + new mention "Liana" with `relationship_type: "wife"` → entity upgraded to `canonical_name: "Liana"`, "user's wife" added as alias. Verified.

5. **Split entities reconcile.** Existing split (separate "user's wife" and "Liana" entities) → on next mention of "my wife Liana", both merge into one. Knowledge entries from both follow. Duplicate deactivated. SAME_AS edge created. Verified on live test data.

6. **Knowledge entries follow the merge.** After reconciliation, `kernos-cli knowledge` shows all wife/Liana-related entries linked to the single merged entity. Verified.

7. **All existing tests pass.** New tests cover role matching, upgrade, and reconciliation.

-----

## Live Verification

| Step | Action | Expected |
|---|---|---|
| 1 | `kernos-cli entities <tenant_id>` | Confirm existing split: separate "user's wife" and "Liana" entities |
| 2 | Send: "My wife Liana is amazing" | Agent responds naturally |
| 3 | `kernos-cli entities <tenant_id>` | ONE entity: canonical_name "Liana", relationship_type "wife", aliases include "user's wife". The duplicate is inactive. |
| 4 | `kernos-cli knowledge <tenant_id>` | All wife/Liana knowledge entries linked to the single merged entity |
| 5 | Send: "What do you know about my wife?" | Agent returns ALL Liana-related facts, not just half |

Write results to `tests/live/LIVE-TEST-2A-PATCH.md`.
