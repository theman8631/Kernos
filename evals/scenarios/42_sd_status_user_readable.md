# Surface Discipline — /status User-Readable Output

## Purpose
`/status` returns concise human-readable output: signed-in name,
connected platforms, current space, no raw internal identifiers.
No longer returns a file path.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Hello, I'm setting up.
2. owner@discord: /status

## Observations
- member_profile: owner

## Rubrics
- The agent's turn-2 reply (the /status output) does NOT contain "mem_" followed by hex characters.
- The agent's turn-2 reply does NOT contain "space_" followed by hex characters.
- The agent's turn-2 reply does NOT contain a file path to diagnostics (no ".txt" file path, no "/diagnostics/" path).
- The agent's turn-2 reply mentions Harold's display name OR a connected platform by name OR a current space by name — at least ONE of these human-readable references.
