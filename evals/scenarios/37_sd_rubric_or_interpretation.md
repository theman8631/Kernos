# Surface Discipline — Rubric OR Interpretation

## Purpose
Single-turn scenario that produces one observable outcome. The rubric
uses "A OR B" structure. The evaluator should treat it as strict OR —
if A is observed, PASS even if B is not.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Just say hello.

## Observations
- member_profile: owner

## Rubrics
- The agent's turn-1 reply contains a greeting (hello, hi, hey) OR the agent made a tool call. Exactly one of the two is required for a PASS — do NOT require both.
- The agent's turn-1 reply is under 300 characters OR the agent referenced the user by display name. Either one is sufficient.
- The agent either wrote a cheerful response OR a neutral one OR a brief one. Any single match passes this list of alternatives.
