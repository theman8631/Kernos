# Repo Cleanup — Align Documentation with Current State

Everything below is one commit. The goal: any agent or human reading the repo 
gets accurate information. Stale planning docs get marked historical. 
Active planning lives in Notion now, not the repo.

## 1. DECISIONS.md — Strip stale planning, keep execution bridge

The NOW block is stale. Future Considerations and Open Questions sections 
contain planning that now lives in Notion. Strip them.

Update NOW block to:
```
## NOW

**Status:** Phase 2 COMPLETE. Phase 3 starting. SPEC-3A (Per-Space File System) in review.
**Owner:** Architect
**Action:** SPEC-3A under Kit review. When approved, moves to Ready for Claude Code.
**Tests:** 633+
**Planning:** All roadmap planning is in Notion. This file is the execution bridge only.
```

Remove the "Open Questions" section entirely.
Remove the "Future Considerations" section entirely.
These are now tracked in Notion's Phase 3 Planning page.

Update the file header to clarify scope:
```
> **What this file is:** The execution bridge between planning and implementation. 
> Claude Code reads this file first, then executes the Active Spec. 
> **Planning and roadmap decisions live in Notion** — not here. 
> This file tracks: current status (NOW block), phase completion (tracker), 
> and architectural decisions made (decision log).
```

Keep: NOW block, Phase Status Tracker (all phases), Decision Log entries.

## 2. docs/BLUEPRINT.md — Mark as historical

Add this block at the very top of the file, before everything else:
```
> ⚠️ **HISTORICAL DOCUMENT — February 2026**
> 
> This is the original vision document for KERNOS. The vision paragraphs 
> remain accurate but implementation details (phases, specific technologies 
> like MemOS/MemCubes, component names) have evolved significantly.
> 
> For current architecture: see `docs/TECHNICAL-ARCHITECTURE.md`
> For current planning: see Notion workspace
> For current decisions: see `DECISIONS.md`
```

Do NOT modify the rest of the Blueprint content — it's historical record.

## 3. docs/ARCHITECTURE-NOTEBOOK.md — Mark as historical

Add at the top:
```
> ⚠️ **HISTORICAL DOCUMENT — Phases 1A through early Phase 2**
> 
> Design rationale and brainstorming from early development. Some sections 
> (spawning decision model, kernel primitive definitions) remain current. 
> Others (context space routing discussion, inline annotation) describe 
> approaches that were superseded.
> 
> For current design decisions: see Notion Kit Reviews and Session Notes
> For rejected approaches: see Notion Rejected Approaches page
```

## 4. docs/CONTEXT-SPACES-DESIGN.md — Mark as superseded

Add at the top:
```
> ⚠️ **SUPERSEDED — March 2026**
> 
> This design was replaced by the Context Routing v0.3 design (founder + Kit), 
> which introduced the LLM router, tagged message stream, and two-gate space 
> creation. The v0.3 design is implemented in SPEC-2B-v2.
> 
> For the current design: see `specs/completed/SPEC-2B-v2-CONTEXT-ROUTING.md`
> For the canonical v0.3 design document: see Notion workspace
```

## 5. Move uncommitted specs to completed/

```bash
mv specs/SPEC-2A-PATCH-ROLE-LINKING.md specs/completed/
mv specs/SPEC-2C-COMPACTION.md specs/completed/
```

## 6. Update README.md

Update the Documentation table to reflect the new landscape:

```markdown
## Documentation

| Document | Purpose |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Current status and decision log. **Start here.** |
| [docs/TECHNICAL-ARCHITECTURE.md](docs/TECHNICAL-ARCHITECTURE.md) | As-built architecture — what exists in code right now |
| [docs/KERNEL-ARCHITECTURE-OUTLINE.md](docs/KERNEL-ARCHITECTURE-OUTLINE.md) | Kernel design: five primitives, three operational modes |
| [specs/completed/](specs/completed/) | All completed implementation specs |

### Planning

Active planning, roadmap, and design discussions live in the **Notion workspace** — not in the repo. 
The repo contains execution artifacts (specs, decisions, code). Notion contains planning artifacts 
(roadmap, reviews, session notes, open questions).

### Historical Reference

| Document | Purpose |
|---|---|
| [docs/BLUEPRINT.md](docs/BLUEPRINT.md) | Original vision document (Feb 2026) — vision is current, implementation details evolved |
| [docs/ARCHITECTURE-NOTEBOOK.md](docs/ARCHITECTURE-NOTEBOOK.md) | Design rationale from Phases 1A–2 — some sections current, some superseded |
| [docs/CONTEXT-SPACES-DESIGN.md](docs/CONTEXT-SPACES-DESIGN.md) | Pre-v0.3 context space design — superseded by SPEC-2B-v2 |
| [research/](research/) | Phase 2 preparation research papers |
```

Update the Status line to:
```
**Status:** Phase 2 complete. 633+ tests. Phase 3 (Agent Workspace) starting.
Active planning in [Notion workspace](https://notion.so).
```

## 7. CLAUDE.md — Update if it exists

If CLAUDE.md contains instructions for Claude Code, update it to:
- Reference DECISIONS.md NOW block as the starting point
- Reference Notion for planning context (Claude Code doesn't read Notion directly, 
  but the spec it receives will have been shaped there)
- Remove any references to BLUEPRINT.md as "single source of truth"
- Confirm TECHNICAL-ARCHITECTURE.md as the as-built reference

## 8. Verify

After all changes:
- `grep -r "single source of truth" docs/` should only match BLUEPRINT.md 
  (inside the historical document, not as a live claim)
- `grep -r "MemOS\|MemCube" docs/TECHNICAL-ARCHITECTURE.md` should return nothing
- `ls specs/` should have no completed specs sitting outside completed/
- `head -5 DECISIONS.md` should show the updated NOW block
