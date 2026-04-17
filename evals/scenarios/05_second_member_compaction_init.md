# Second Member Compaction Init

## Purpose
Regression guard: when a second member's space is initialized for the first
time, `compute_document_budget` must be called with the correct argument
shape `(MODEL_MAX_TOKENS, 4000, 0, DEFAULT_DAILY_HEADROOM)`. Earlier a
missing arg crashed second-member init silently — the first member worked
fine but the second one failed to get a compaction budget, breaking their
compaction pipeline.

This scenario creates two members, sends each enough turns to trigger
compaction, and verifies both produced knowledge/harvest output without
erroring out.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"
  - id: emma
    display_name: Emma
    role: member
    platform: telegram
    channel_id: "2000000002"

## Turns
1. owner@discord: Hey, just got this set up.
2. owner@discord: I'm working on Kernos today.
3. owner@discord: Think I'll focus on the eval harness this morning.
4. emma@telegram: Hi, Emma here.
5. emma@telegram: Harold mentioned you might help me with scheduling.
6. emma@telegram: I'm trying to block out focused time for writing this week.
7. emma@telegram: Mornings are best for me, afternoons are for meetings.
8. owner@discord: Back. Made progress on the scenario parser.
9. emma@telegram: Thanks. Talk later.

## Observations
- knowledge: owner
- knowledge: emma
- member_profile: owner
- member_profile: emma
- conversation_log: owner
- conversation_log: emma

## Rubrics
- Neither turn crashed with an exception (no turn_result.error strings).
- Emma's knowledge entries list is non-empty after her turns (her compaction fired and produced at least one extracted fact).
- Emma's member_profile exists and has display_name="Emma".
- Harold's knowledge entries are distinct from Emma's (no cross-contamination: Harold's entries don't mention scheduling/writing; Emma's don't mention the eval harness).
