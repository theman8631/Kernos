"""Cohorts — specialist LLM work the kernel delegates on the primary agent's behalf.

Cohorts are infrastructure, not agent-facing capabilities. Agents don't choose
to invoke them; the kernel dispatches them algorithmically at the right moment.
The agent does not see a cohort's existence, its outputs, or its decisions —
the kernel consumes the cohort's output and shapes downstream behavior from it.

Per the Judgment-vs-Plumbing principle: cohorts own judgment; everything else
(pair tracking, covenant lookup, dispatch, observability, target resolution)
is Python and agent-invisible.

Members of this package:

* ``messenger`` — welfare-first cross-member disclosure cohort. Runs on every
  RM-permitted cross-member exchange. Judges whether the content serves the
  disclosing member's welfare given their covenants, disclosures, and the
  relationship profile. Always-respond; confident-omission-or-refer.
"""
