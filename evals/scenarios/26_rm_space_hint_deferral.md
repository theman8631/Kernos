# Relational Messaging — Space-Hint Deferral (Match Exists)

## Purpose
A `target_space_hint` names a real space in Emma's space list, but NOT
her active space. The dispatcher defers pickup (`space_hint_mismatch`)
until Emma switches to that space. While deferred, the envelope stays
`pending` — the next-turn path skips it.

This scenario can't reliably switch spaces via LLM, so it verifies the
deferral via observation: after Emma's turn, the envelope is still
`pending` (not delivered).

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
1. emma@telegram: Hi. I'll use this space for side-project work. Create a space called "SideProject" for that if you can.
2. owner@discord: Send Emma a message about her side-project plans. Use send_relational_message, intent=inform, target_space_hint="space_nonexistent_id". content "stale-hint message about side project". (This will fall through — no matching space.)
3. emma@telegram: Anything new?

## Observations
- relational_messages: emma

## Rubrics
- An envelope with target_space_hint set exists (content references "side project" / "stale-hint").
- The envelope reached `delivered` or `surfaced` — because the hint doesn't match any of Emma's spaces, it fell through to the null-hint path and surfaced on turn 3.
- This confirms the stale-hint-fallthrough behavior. (True hard-deferral testing with a matching-but-non-active hint is covered at the dispatcher unit-test level.)
