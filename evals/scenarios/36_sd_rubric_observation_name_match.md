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
- kind: mechanical
  check: observation_has
  observation: relationships:owner
  where:
    declarer_display_name: Harold
    other_display_name: Emma
- kind: mechanical
  check: observation_has
  observation: relationships:emma
  where:
    declarer_display_name: Emma
    other_display_name: Harold
- kind: mechanical
  check: observation_field_equals
  observation: member_profile:owner
  field: display_name
  value: Harold
- kind: mechanical
  check: observation_field_equals
  observation: member_profile:emma
  field: display_name
  value: Emma
