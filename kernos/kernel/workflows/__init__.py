"""Workflow loop primitive — trigger registry, workflow registry,
execution engine, action library, ledger.

Per SPEC-WORKFLOW-LOOP-PRIMITIVE: workflows are action-loop instances
fired by triggers that match events on the shipped event_stream
substrate. No parallel event bus; trigger_registry attaches via the
post-flush hook shipped in C1.
"""
