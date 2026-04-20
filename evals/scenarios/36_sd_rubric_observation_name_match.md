# Surface Discipline — Rubric Observation Name Match

## Purpose
Observations now attach display_name alongside scenario-id. A rubric
that references "Harold" by display name should match the observation
output deterministically.

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

## Observations
- relationships: owner
- relationships: emma
- member_profile: owner
- member_profile: emma

## Rubrics
- The `relationships:owner` observation includes an entry whose `declarer_display_name` equals "Harold" AND whose `other_display_name` equals "Emma".
- The `relationships:emma` observation includes an entry whose `declarer_display_name` equals "Emma" AND whose `other_display_name` equals "Harold".
- The `member_profile:owner` observation has `display_name` equal to "Harold".
- The `member_profile:emma` observation has `display_name` equal to "Emma".
