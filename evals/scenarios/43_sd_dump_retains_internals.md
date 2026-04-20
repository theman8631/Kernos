# Surface Discipline — /dump Retains Raw Internals

## Purpose
Complement to scenario 42. `/dump` is an admin/diagnostic surface — raw
`mem_xxx` / `space_xxx` identifiers and file paths are retained by design
(developers need them). The sanitizer does NOT fire on this path, and no
SURFACE_LEAK_DETECTED warning is triggered.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Hi, initialize me.
2. owner@discord: /dump

## Observations
- member_profile: owner

## Rubrics
- The agent's turn-2 reply (the /dump output) contains a diagnostic-style output — either a file path (containing "/diagnostics/" or ending in ".txt"), OR raw context dump markers like "SYSTEM PROMPT" / "MESSAGES" / "TOOLS" / "tokens". Any admin-tool indicator passes.
- The turn-2 reply does NOT pretend to be user-friendly conversational output — it's a diagnostic handoff.
- Raw internal identifiers being present in /dump output is ACCEPTABLE and expected here (not a failure).
