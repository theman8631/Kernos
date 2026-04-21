# Surface Discipline — /wipe Confirmation Gate

## Purpose
`/wipe me` alone prompts for confirmation. `/wipe me yes` proceeds.
Anything else cancels. Verifies the D5 gate is in place and the legacy
exact-phrase path is still present as a fallback.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: /wipe me
2. owner@discord: /wipe me no
3. owner@discord: /wipe me

## Observations
- member_profile: owner

## Rubrics
- The agent's turn-1 reply asks for explicit confirmation and mentions "/wipe me yes" (or similar) as the way to proceed. It does NOT execute the wipe on turn 1.
- The agent's turn-2 reply indicates cancellation ("cancelled" or "not wiping" or similar). It does NOT execute the wipe.
- The agent's turn-3 reply again prompts for confirmation — turn-2's `no` reset any pending state.
- kind: mechanical
  check: observation_field_equals
  observation: member_profile:owner
  field: display_name
  value: Harold
