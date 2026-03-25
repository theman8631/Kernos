"""Thin index for Kernos documentation.

The full docs live in docs/ and are accessed via read_doc(path).
This module provides the DOCS_HINT for the system prompt and any
always-in-prompt items too small for their own doc file.
"""

DOCS_HINT = """\
Your documentation is in docs/. Use read_doc(path) to understand any capability or behavior.

Key sections:
- docs/index.md — overview and quick directory
- docs/capabilities/overview.md — what tools are available
- docs/capabilities/web-browsing.md — how to search the web
- docs/capabilities/calendar.md — calendar tools
- docs/capabilities/file-system.md — per-space files
- docs/capabilities/memory-tools.md — remember() and knowledge retrieval
- docs/behaviors/covenants.md — behavioral rules
- docs/behaviors/dispatch-gate.md — what gets confirmed vs executed
- docs/behaviors/proactive-awareness.md — whispers and time-sensitive signals
- docs/behaviors/instruction-types.md — behavioral constraints vs automation rules
- docs/architecture/context-spaces.md — how spaces work
- docs/architecture/memory.md — knowledge extraction and retrieval
- docs/architecture/soul.md — identity model
- docs/identity/who-you-are.md — platform identity
- docs/roadmap/vision.md — where this is going
"""
