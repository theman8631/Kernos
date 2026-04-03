"""Thin index for Kernos documentation.

The full docs live in docs/ and are accessed via read_doc(path).
This module provides the DOCS_HINT for the system prompt and any
always-in-prompt items too small for their own doc file.
"""

DOCS_HINT = """\
Your documentation is in docs/. Use read_doc('docs/index.md') for the full directory \
when you need reference material."""
