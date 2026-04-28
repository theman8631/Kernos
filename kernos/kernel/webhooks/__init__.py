"""Webhook receiver — external sources translate inbound HTTP POST
payloads into ``event_stream.emit(event_type="external.webhook", ...)``
calls so workflow triggers can fire on third-party events.

Per WORKFLOW-LOOP-PRIMITIVE C6.
"""
