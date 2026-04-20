# Relational Messaging — Multi-Turn Threaded Coordination

## Purpose
Three intents (ask_question → inform → request_action) chained via a
shared conversation_id across three turn exchanges. Thread integrity
holds end-to-end.

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
1. owner@discord: Declare full-access toward Emma.
2. emma@telegram: Declare full-access toward Harold.
3. owner@discord: send_relational_message to Emma. intent=ask_question. content "thread-M step1: are you free for 30 min on Thursday?"
4. emma@telegram: Any messages? If so, reply in the same thread via send_relational_message. intent=inform. content "thread-M step2: Thursday works, pick a time between 1pm and 4pm". Use the conversation_id from the incoming message.
5. owner@discord: Anything in? Reply to Emma in the same thread — intent=request_action, content "thread-M step3: please book 2pm Thursday". Use the conversation_id.
6. emma@telegram: Any new messages for me?

## Observations
- relational_messages: owner
- relational_messages: emma

## Rubrics
- At least THREE envelopes exist in the combined relational_messages observations (step1, step2, step3).
- All three share the SAME conversation_id.
- The intents in order are: ask_question (step1), inform (step2), request_action (step3).
- The final envelope (step3) is delivered/surfaced/resolved — it reached Emma's turn 6.
