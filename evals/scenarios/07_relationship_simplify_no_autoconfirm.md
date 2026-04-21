# Relationship Simplify — No Auto-Confirmation

## Purpose
Harold declares Emma as `full-access`; Emma then declares Harold as
`no-access`. Each side is stored independently. Verify no auto-confirmation
or state-machine flip occurs: Harold's declaration does NOT change Emma's
side, and vice versa. The two sides are allowed to disagree because
"a declaration is what the declaring member wants."

Corresponds to RELATIONSHIP-SIMPLIFY expected behaviors:
- Each side is stored independently.
- No auto-flip when the other party declares back.
- `list_relationships` reflects per-side declarations.

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
1. owner@discord: Please declare Emma as full-access for me.
2. emma@telegram: Please declare Harold as no-access for me.
3. owner@discord: List my relationships again.
4. emma@telegram: List my relationships.

## Observations
- member_profile: owner
- member_profile: emma
- relationships: owner
- relationships: emma

## Rubrics
- kind: mechanical
  check: observation_has
  observation: relationships:owner
  where:
    declarer: owner
    other: emma
    permission: full-access
- kind: mechanical
  check: observation_has
  observation: relationships:emma
  where:
    declarer: emma
    other: owner
    permission: no-access
- The two declarations do NOT have matching permissions — if auto-flip were happening, both rows would have the same permission. They don't, which confirms no auto-confirmation.
- No reply in the transcript uses the legacy status words "confirmed", "proposed", or "disputed" when describing the declarations (these terms referred to the old auto-confirmation state machine, which was removed).
- No reply uses the legacy profile names: `full-share`, `work-only`, `coordination-only`, `minimal`.
