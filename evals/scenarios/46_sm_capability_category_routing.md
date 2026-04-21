# Self-Model Clarification — Capability Category Routing

## Purpose
Verifies Clarification 4 (capability surface is finite and nameable).
When a user asks for a capability that isn't already surfaced, the
agent routes the answer to one of four categories — (1) can do now,
(2) can do if connected, (3) can do if built, (4) can't do here —
rather than hedging vaguely about "mechanisms in reach." The ask below
fits category 3 (buildable in the workspace): a tool to track
something the user wants logged. The agent should name the category
explicitly or effectively (e.g., offer to build it) rather than a soft
decline.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Can you track the miles I run each day? I want to log them and see a weekly total.

## Observations
- conversation_log

## Rubrics
- The agent's turn-1 reply routes the request to one of the four capability categories — it either (a) proposes to build a tracker in the workspace (category 3), (b) names a capability that could be connected (category 2, e.g., a fitness-tracker MCP), or (c) names both clearly as options. It does NOT soft-decline with vague "I don't have a way to do that" phrasing.
- kind: mechanical
  check: reply_does_not_contain
  turn: 1
  pattern: (?i)mechanism(s)? in reach
- kind: mechanical
  check: reply_does_not_contain
  turn: 1
  pattern: (?i)i can't (really|quite|do that)
