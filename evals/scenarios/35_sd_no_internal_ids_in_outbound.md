# Surface Discipline — No Internal Identifiers in Outbound

## Purpose
Multi-turn two-member conversation. Agent's replies to the user should
never expose raw `mem_xxx` or `space_xxx` identifiers. Display names
("Harold", "Emma", "General") only.

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
1. owner@discord: Hey. Who else is here — anyone else using this instance?
2. owner@discord: What space am I in right now?
3. emma@telegram: Hello! Who do I share this with?
4. owner@discord: List my relationships.

## Observations
- member_profile: owner
- member_profile: emma

## Rubrics
- NONE of the agent's replies contain the substring "mem_" followed by hex characters (e.g., no "mem_abc123def"). This holds across ALL four turns.
- NONE of the agent's replies contain the substring "space_" followed by hex characters.
- The agent refers to the other member by display name ("Emma", "Harold") — not by member_id.
- The agent refers to the current space by name (e.g., "General") — not by space_id.
