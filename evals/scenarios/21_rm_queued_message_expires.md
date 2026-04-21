# Relational Messaging — Queued Message Expires

## Purpose
A `normal` urgency message sits in the queue past its 72h TTL. The
expiration sweep transitions it to `expired` (terminal). Verified via
an `rm_backdate` action turn (harness primitive) to fast-forward its
created_at past the TTL, then `rm_sweep_expired` to run the sweep.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. content "stale queued note".
2. action: system rm_backdate match=stale queued note, seconds_ago=400000
3. action: system rm_sweep_expired

## Observations
- relational_messages: owner
- relational_messages: emma

## Rubrics
- The envelope "stale queued note" appears in `relational_messages:owner`.
- kind: mechanical
  check: observation_has
  observation: relational_messages:owner
  where:
    state: expired
- `expired_at` is non-empty.
- No `delivered_at` is set — it never reached Emma's turn before expiring.
