# Relational Messaging — Delivery State Atomicity

## Purpose
Time-sensitive push transitions pending→delivered on send. When Emma's
turn picks up messages, the dispatcher's CAS ensures the already-
delivered envelope isn't re-transitioned — next-turn pickup sees
`delivered` (not `pending`) and no double-transition occurs.

Complements the unit-test coverage of CAS atomicity
(test_relational_messaging_storage, test_relational_dispatch) with a
live-path assertion.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=time_sensitive. content "atomic-test — the 2pm sync moved to 3pm".
2. emma@telegram: Any notes?

## Observations
- relational_messages: emma

## Rubrics
- `relational_messages:emma` has exactly ONE envelope (the test doesn't re-transition anything).
- The envelope's state is `surfaced` by end of scenario (push delivered it, next-turn pickup saw it already delivered, persist marked it surfaced).
- `delivered_at` is non-empty and is earlier than (or equal to, if same instant) `surfaced_at`.
- No duplicate envelope with the same content exists.
