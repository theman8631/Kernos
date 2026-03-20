# CLAUDE.md — Instructions for Claude Code

## Project: KERNOS

A personal agentic operating system. As-built architecture: `docs/TECHNICAL-ARCHITECTURE.md`.

## Before You Do Anything

1. **Read `DECISIONS.md` first.** The NOW block tells you what to do. It contains the Active Spec you should execute and recent architectural decisions. If something in DECISIONS.md conflicts with other documents, DECISIONS.md wins (it's more recent).
2. **Read `docs/TECHNICAL-ARCHITECTURE.md`** for as-built architecture — components, data flows, interfaces. (`docs/BLUEPRINT.md` is a historical vision document, not the current reference.)
3. **Execute only the Active Spec** in DECISIONS.md. Don't jump ahead to future phases. Don't build things not in the current spec. Planning lives in Notion; specs come to you via DECISIONS.md.

## Kernel Architecture Context

For Phase 1B work, read `docs/KERNEL-ARCHITECTURE-OUTLINE.md` for the kernel design vision. Key conventions for the kernel layer (`kernos/kernel/`):

- **Event emission is best-effort.** Every `emit()` call is wrapped in try/except. Event logging failures never break the user's message flow.
- **State Store is the query surface.** Runtime lookups go to the State Store, not the Event Stream. The Event Stream is for append, replay, and audit.
- **Concurrency:** JSON-on-disk uses `filelock` for single-process safety. Not safe for multi-worker. The abstract interfaces allow swapping backends.
- **Shadow archive:** No method permanently deletes data. "Removal" sets `active: false`.
- **Cost logging:** Every reasoning call logs model, tokens, estimated cost, duration via events.

## Architectural Constraints (Always Enforced)

These are non-negotiable. Violating any of these is a build failure regardless of what the Active Spec says:

- **Adapter/handler isolation:** The handler NEVER imports from adapters. Adapters NEVER import from the handler. They share only the NormalizedMessage model.
- **tenant_id from day one:** Every piece of state is keyed to a `tenant_id`. No code ever assumes a single user.
- **No destructive deletions:** Every "delete" is a relocation to a shadow archive. No operation permanently destroys user data.
- **Graceful errors:** Every failure mode produces a friendly user-facing response. Never a silent crash, never a raw exception.
- **MCP for capabilities:** Tools and data are accessed through MCP. No direct API integrations that bypass the capability abstraction layer.

## When You're Done

- Run the acceptance criteria listed in the Active Spec. All must pass.
- Run `pytest` — all tests green.
- Verify architectural constraints (especially import isolation — grep for it).
- Update the relevant `docs/` section to reflect any new or changed components from this spec. The docs are the canonical reference for how the system works.
- Do NOT update DECISIONS.md status yourself. The founder and architect handle that.

## Code Style

- Python 3.11+
- Type hints on all function signatures
- `logging` module for all logging (no print statements)
- Docstrings on public classes and functions
- Keep it simple — no premature abstraction beyond what the spec requires