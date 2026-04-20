# Surface Discipline — Multi-Step Continuation

## Purpose
A single user request requires three sequential tool calls. The agent
should complete all three before transitioning to a conversational
response. Targets the under-continuation failure mode (scenario 34 RED
pattern: agent sends step 1 and stops).

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
  - id: jamie
    display_name: Jamie
    role: member
    platform: discord
    channel_id: "3000000003"

## Turns
1. owner@discord: Send the same short note to Emma, Jamie, and back to me in a confirmation. Use send_relational_message. Three separate messages. The note is: "status update: Q3 plan moved to next Monday".

## Observations
- relational_messages: owner

## Rubrics
- `relational_messages:owner` contains at least TWO envelopes where `origin` is "owner" (Harold). (Harold asked for one to Emma AND one to Jamie — minimum two relational messages.)
- One envelope has `addressee` resolving to Emma (either `addressee` equals "emma" OR `addressee_display_name` equals "Emma").
- One envelope has `addressee` resolving to Jamie (either `addressee` equals "jamie" OR `addressee_display_name` equals "Jamie").
- The agent's turn-1 reply confirms BOTH sends happened — mentions Emma AND Jamie by name.
