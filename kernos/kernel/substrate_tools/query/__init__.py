"""Read/query surfaces for SubstrateTools.

All query surfaces are deterministic, instance-scoped, and contain no
LLM calls. They proxy to shipped substrate (DAR, WLP, WDP) and aggregate
provider/context data through neutral registries.
"""
from __future__ import annotations
