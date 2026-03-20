# SPEC-3I: Soul Retirement + Clean Hatch — COMPLETE

**Status:** COMPLETE
**Implemented:** 2026-03-19
**Tests:** Covered in test_self_knowledge.py (soul defaults, bootstrap prompt, migration, read_soul/update_soul)

## What Was Done

- **SOUL.md deleted** — no more static personality file in the repo root
- **read_soul / update_soul kernel tools** — agent reads and updates soul.json directly via state store. Updatable fields: agent_name, emoji, personality_notes, communication_style. Lifecycle fields (hatched, interaction_count, bootstrap_graduated) and user_name are read-only.
- **Soul defaults** — new souls start with agent_name="Kernos", emoji="🜁". Migration backfills empty values on first read.
- **Bootstrap prompt updated** — "You are Kernos" + "Don't narrate your own state". Discovery-oriented, competence-first onboarding.
- **developer_mode flag** on TenantProfile for developer-facing features.

## Verification

- `SOUL.md` does not exist in repo root
- `read_soul` returns soul.json fields as JSON
- `update_soul` accepts field+value, rejects lifecycle fields
- New souls default to Kernos/🜁
- Bootstrap prompt contains "You are Kernos" and "Don't narrate your own state"
- All tests pass
