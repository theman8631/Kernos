# Surface Discipline — Rubric AND Interpretation

## Purpose
Single-turn scenario. Rubric uses "A AND B" structure. The evaluator
should treat it as strict AND — both conditions required to PASS.
Paired with scenario 37 to prove the evaluator distinguishes the two.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Say hello.

## Observations
- member_profile: owner

## Rubrics
- The agent's turn-1 reply contains a greeting word (hello, hi, hey) AND is under 500 characters. BOTH are required; if either fails, FAIL the rubric.
- The agent's turn-1 reply is in English AND is non-empty. Both required.
- The agent did NOT fabricate a tool call AND did NOT refer to the user by a fabricated internal id (mem_xxx). Both conditions required.
