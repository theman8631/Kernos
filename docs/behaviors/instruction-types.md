# Instruction Types

When a user gives a behavioral instruction, the Tier 2 extractor classifies it into one of two types. This determines how the instruction is stored and enforced.

## Behavioral Constraints → Covenant Rules

Instructions that constrain how the agent behaves become `CovenantRule` records:

- "Never email my ex" → `must_not` rule on email capability
- "Always confirm before spending money" → `escalation` rule on cost-bearing actions
- "Don't schedule meetings before 9am" → `must_not` rule on calendar
- "Be more formal in work conversations" → `preference` rule

These are enforced by the dispatch gate. The agent sees them in its system prompt and the gate model evaluates tool calls against them.

## Automation Rules → Standing Orders

Instructions that describe automated actions become `standing_order` knowledge entries:

- "Every Monday, send me a summary of the week ahead"
- "When someone emails about invoices, flag it for me"
- "Check my calendar every morning and tell me what's coming up"

Standing orders are stored as knowledge entries with `category="pattern"` and a standing order marker. They are NOT yet enforced by an automated trigger system — that is a decided next step (unified trigger system).

Currently, standing orders serve as remembered instructions that the agent can recall via `remember` and act on when the context is right. Full automated execution awaits the trigger system.

## Classification

The NL Contract Parser (`kernos/kernel/contract_parser.py`) handles classification:

- `classify_and_parse()` returns a `ParseResult` with `instruction_type` ("behavioral_constraint" or "automation_rule"), plus the structured rule or standing order

## Code Locations

| Component | Path |
|-----------|------|
| Contract parser | `kernos/kernel/contract_parser.py` |
| CovenantRule | `kernos/kernel/state.py` |
| KnowledgeEntry (standing orders) | `kernos/kernel/state.py` |
