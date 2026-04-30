"""Universal cohort substrate.

Reusable building blocks for *system cohorts* — long-running tool-starved
cohorts that observe the event stream between turns, hold scratch state
in shipped substrate (WDP / STS / etc.), and signal the principal cohort
when their work product is ready.

Drafter is the first user. Future Pattern Observer and Curator cohorts
inherit the same patterns from this directory. Three substrate primitives:

* :mod:`tool_restriction` — universal tool whitelist enforcement with
  ``CohortToolForbidden`` typed errors. Belt-and-suspenders for any code
  path that bypasses cohort-specific port surfaces.
* :mod:`cursor` — durable per-cohort, per-instance event-stream cursor
  with at-least-once delivery and per-event commit semantics.
* :mod:`action_log` — crash-idempotent recording of cohort side effects
  (writes, signal emissions, receipts). Composite-keyed by
  ``(cohort_id, instance_id, source_event_id, action_type, target_id)``
  with NOT NULL ``target_id``; same-transaction insert with the side
  effect itself.

The directory placement (``cohorts/_substrate/``) signals the reusable
intent: future cohorts inherit from these primitives without
Drafter-specific assumptions. Reviewers of follow-on specs should reject
changes that introduce Drafter-specific names or semantics into this
module.
"""
from __future__ import annotations
