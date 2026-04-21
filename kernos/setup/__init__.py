"""LLM setup and fallback infrastructure.

This package implements the `kernos setup llm` console flow, the provider
registry, the storage-backend abstraction for secrets, the benchmark
snapshot reader (setup-time only), and the startup binary health check.

Zero-LLM-call invariant: nothing in this package imports an LLM client or
reaches messages.create(). Setup configures the LLM chain — it cannot
depend on the LLM chain.
"""
