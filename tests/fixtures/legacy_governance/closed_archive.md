# GOVERNANCE: Self-review functional map coverage gap

Class: governance
Signature: self-review:coverage-gap
Occurrence: c380addc84ef8f52
Human-gated: true
Opened: 2026-07-01T00:00:00+00:00
Last-seen: 2026-07-01T00:00:00+00:00

## Condition
`unassigned_modules(REVIEW_SLICES)` is non-empty: these modules belong to no element of the functional map and are therefore structurally ineligible for daily self-review. Resolves when every module below is owned by an element.

## Payload
- kernos/a.py
- kernos/b.py

This item is HUMAN-GATED. It is bookkeeping, not authority: no auto-trigger consumes it and nothing here may be self-applied. It stays open until the condition above genuinely clears.
