"""Domain-agent registry — per-instance roster of routable agents.

Per SPEC-DOMAIN-AGENT-REGISTRY: addressability substrate for both
``route_to_agent`` (workflow descriptor → agent_id → inbox) and
conversational routing (natural-language phrase → agent_id → inbox).
The registry stores serializable AgentRecord descriptors; concrete
``AgentInbox`` instances are constructed at dispatch time via a
``ProviderRegistry`` factory keyed on ``provider_key``.
"""
