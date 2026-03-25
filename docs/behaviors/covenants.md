# Covenants

Covenants are behavioral rules that guide agent actions. They define what the agent must do, must not do, prefers, and when to escalate. Every tenant starts with seven default rules. Users add rules through natural conversation.

## How Covenants Are Created

**Covenants are automatically captured by the kernel.** When the user gives a behavioral instruction mid-conversation — "never email my ex", "always confirm before spending money", "don't schedule meetings before 9am" — the Tier 2 extractor detects it and creates a `CovenantRule` record. The agent does NOT need to create rules manually.

This is the sole creation path. The `manage_covenants` tool is for viewing, editing, and removing existing rules — not creating new ones.

## Rule Structure

Each `CovenantRule` (`kernos/kernel/state.py`) has:

- **rule_type** — `must` (required behavior), `must_not` (prohibited), `preference` (soft guidance), `escalation` (when to ask)
- **description** — natural language description of the rule
- **capability** — which tool/capability this rule applies to (or `"general"`)
- **enforcement_tier** — `silent`, `notify`, `confirm`, or `block`
- **context_space** — `None` for global rules, or scoped to a specific space
- **source** — `default` (system), `user_stated`, or `evolved`
- **layer** — `principle` (hard boundary) or `practice` (flexible guidance)

## Default Rules

Every new tenant gets seven default covenants:

1. Confirm before sending messages to external parties
2. Never share the user's personal data with third parties without permission
3. Respect data privacy — don't store sensitive information unnecessarily
4. Flag unexpected costs before proceeding
5. Defer to the user on decisions about their relationships
6. Be concise — prefer brief responses unless detail is requested
7. Escalate when unsure — ask rather than guess

## Post-Write Validation

After every rule creation, `validate_covenant_set()` fires a single Haiku call checking the full set for:

- **SUPERSEDE** — a newer rule replaces an older one on the same topic (the user changed their mind). The older rule is retired automatically. This is the most common resolution for apparent conflicts.
- **MERGE** — auto-resolves duplicate rules (supersedes the older one)
- **CONFLICT** — genuinely ambiguous contradictions surface as a whisper once. If unresolved after 3 validation runs, the older rule is auto-superseded.
- **REWRITE** — auto-improves poorly worded rules

## manage_covenants Tool

| Field | Value |
|-------|-------|
| Effect | soft_write (except "list" action which is read) |
| Actions | `list` — show active rules grouped by type |
| | `remove` — soft-remove a rule (sets active=false) |
| | `update` — create new rule superseding old one (audit trail preserved) |

If you're unsure whether a rule was captured, use `list` to check. Don't try to create rules — the kernel handles that.

## Instruction Classification

The Tier 2 extractor classifies instructions into two types:

- **behavioral_constraint** → becomes a `CovenantRule` (enforced by dispatch gate)
- **automation_rule** → becomes a `standing_order` knowledge entry (not yet enforced by triggers)

## Code Locations

| Component | Path |
|-----------|------|
| CovenantRule dataclass | `kernos/kernel/state.py` |
| NL Contract Parser | `kernos/kernel/contract_parser.py` |
| Covenant validation | `kernos/kernel/covenant_manager.py` |
| MANAGE_COVENANTS_TOOL | `kernos/kernel/covenant_manager.py` |
| Gate enforcement | `kernos/kernel/reasoning.py` (_gate_tool_call) |
