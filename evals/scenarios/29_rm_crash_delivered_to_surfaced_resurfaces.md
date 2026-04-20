# Relational Messaging — Crash Between Delivered and Surfaced Re-Surfaces

## Purpose
If the recipient's agent crashes between `delivered` and `surfaced` —
the envelope transitioned to delivered (agent saw it in context) but
never reached the surfaced commit point — the next turn re-includes
the still-delivered envelope so the user never misses it.

Simulated via an `rm_force_state` action turn that reverts the state
to delivered after turn 2 (simulating the crash). Turn 3 then verifies
re-pickup.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. content "crash-recovery test message about the Monday meeting".
2. emma@telegram: Anything new?
3. action: system rm_force_state match=crash-recovery test, from_state=surfaced, to_state=delivered
4. emma@telegram: Quick check again — anything I missed?

## Observations
- relational_messages: emma

## Rubrics
- The envelope exists in `relational_messages:emma`.
- After turn 4, the envelope's state is `surfaced` again — the re-pickup on turn 4 re-delivered the crash-reverted envelope and persist re-surfaced it. This proves the delivered→surfaced crash recovery works.
- The agent's turn-4 reply references the Monday-meeting message (re-surfacing behavior).
