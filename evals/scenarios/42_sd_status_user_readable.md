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
- kind: mechanical
  check: reply_does_not_contain
  turn: 2
  pattern: 'mem_[a-f0-9]+'
- kind: mechanical
  check: reply_does_not_contain
  turn: 2
  pattern: 'space_[a-f0-9]+'
- kind: mechanical
  check: reply_does_not_contain
  turn: 2
  pattern: '\.txt|/diagnostics/'
- The agent's turn-2 reply mentions Harold's display name OR a connected platform by name OR a current space by name — at least ONE of these human-readable references.
