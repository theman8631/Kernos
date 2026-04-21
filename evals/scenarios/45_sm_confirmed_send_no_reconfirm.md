# Self-Model Clarification — Confirmed Action Completes Without Reconfirm

## Purpose
Verifies Clarification 3 (pair every negative rule with its positive
complement). The paired wording on the money and drafts covenants tells
the agent: *"the confirmation is the authorization event — don't
re-confirm."* On an explicit pre-confirmed request, the agent completes
the call rather than looping back to "are you sure?"

Uses `manage_schedule` (always-available kernel tool, no member
disambiguation required) so the test isolates the confirmation-completion
behavior.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Create a reminder via manage_schedule: "call Emma" at 5pm tomorrow. I'm explicitly confirming — create it now, no further checks.

## Observations
- conversation_log

## Rubrics
- kind: mechanical
  check: tool_called
  tool_name: manage_schedule
- kind: mechanical
  check: reply_does_not_contain
  turn: 1
  pattern: (?i)are you sure
- kind: mechanical
  check: reply_does_not_contain
  turn: 1
  pattern: (?i)should i (create|add|proceed)
- kind: mechanical
  check: reply_does_not_contain
  turn: 1
  pattern: (?i)want me to (go ahead|proceed|create)
- The agent's turn-1 reply reads as completion (e.g., "reminder set", "done") rather than asking for a confirmation that was already given.
