"""Tests for Kernos Self-Knowledge Reference System.

Covers: read_source tool (security, file reading, section extraction),
kernos-reference.md provisioning, and tool registration.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.reasoning import (
    READ_SOURCE_TOOL,
    ReasoningService,
    _read_source,
)


# ---------------------------------------------------------------------------
# read_source: Valid paths
# ---------------------------------------------------------------------------


class TestReadSourceValidPaths:
    def test_read_existing_file(self):
        result = _read_source("kernel/awareness.py")
        assert "AwarenessEvaluator" in result
        assert "class AwarenessEvaluator" in result

    def test_read_reasoning_module(self):
        result = _read_source("kernel/reasoning.py")
        assert "ReasoningService" in result

    def test_read_handler(self):
        result = _read_source("messages/handler.py")
        assert "MessageHandler" in result

    def test_read_event_types(self):
        result = _read_source("kernel/event_types.py")
        assert "PROACTIVE_INSIGHT" in result

    def test_read_init(self):
        result = _read_source("__init__.py")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# read_source: Security
# ---------------------------------------------------------------------------


class TestReadSourceSecurity:
    def test_reject_path_traversal(self):
        result = _read_source("../secrets/api.key")
        assert "Error" in result
        assert "not allowed" in result

    def test_reject_absolute_path(self):
        result = _read_source("/etc/passwd")
        assert "Error" in result
        assert "Absolute paths" in result

    def test_reject_double_dot_in_middle(self):
        result = _read_source("kernel/../../../etc/passwd")
        assert "Error" in result

    def test_reject_backslash_absolute(self):
        result = _read_source("\\etc\\passwd")
        assert "Error" in result

    def test_nonexistent_file(self):
        result = _read_source("kernel/nonexistent_module.py")
        assert "Error" in result
        assert "not found" in result

    def test_reject_binary_file(self):
        # .pyc files should be rejected
        result = _read_source("__pycache__/something.pyc")
        if "not found" not in result:
            assert "Error" in result

    def test_directory_rejected(self):
        result = _read_source("kernel")
        assert "Error" in result
        assert "Not a file" in result


# ---------------------------------------------------------------------------
# read_source: Section extraction
# ---------------------------------------------------------------------------


class TestReadSourceSections:
    def test_extract_class(self):
        result = _read_source("kernel/awareness.py", "AwarenessEvaluator")
        assert "class AwarenessEvaluator" in result
        assert "def __init__" in result
        assert "run_time_pass" in result
        # Should NOT contain Whisper class (different section)
        assert "class Whisper" not in result

    def test_extract_method(self):
        result = _read_source("kernel/awareness.py", "run_time_pass")
        assert "async def run_time_pass" in result
        assert "foresight_expires" in result
        # Should be a focused extraction, not the whole file
        assert "class AwarenessEvaluator" not in result

    def test_extract_async_def(self):
        result = _read_source("kernel/awareness.py", "_evaluate")
        assert "async def _evaluate" in result

    def test_extract_format_time_insight(self):
        result = _read_source("kernel/awareness.py", "_format_time_insight")
        assert "def _format_time_insight" in result
        assert "urgency" in result

    def test_section_not_found(self):
        result = _read_source("kernel/awareness.py", "NonExistentClass")
        assert "Error" in result
        assert "not found" in result

    def test_extract_read_source_itself(self):
        result = _read_source("kernel/reasoning.py", "_read_source")
        assert "def _read_source" in result
        assert "Security" in result or "security" in result.lower()


# ---------------------------------------------------------------------------
# read_source: Truncation
# ---------------------------------------------------------------------------


class TestReadSourceTruncation:
    def test_large_file_truncated(self):
        result = _read_source("messages/handler.py")
        # handler.py is >500 lines, should be truncated
        assert "truncated" in result.lower()

    def test_small_file_not_truncated(self):
        result = _read_source("kernel/event_types.py")
        # event_types.py is <500 lines
        assert "truncated" not in result.lower()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestReadSourceToolDefinition:
    def test_tool_shape(self):
        assert READ_SOURCE_TOOL["name"] == "read_source"
        schema = READ_SOURCE_TOOL["input_schema"]
        assert "path" in schema["properties"]
        assert "section" in schema["properties"]
        assert schema["required"] == ["path"]

    def test_in_kernel_tools(self):
        assert "read_source" in ReasoningService._KERNEL_TOOLS

    def test_classified_as_read(self):
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        assert r._classify_tool_effect("read_source", None) == "read"


# ---------------------------------------------------------------------------
# kernos-reference.md
# ---------------------------------------------------------------------------


class TestKernosReference:
    def test_reference_content_exists(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert len(KERNOS_REFERENCE) > 1000

    def test_covers_context_spaces(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Context Spaces" in KERNOS_REFERENCE

    def test_covers_memory_knowledge(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Memory & Knowledge" in KERNOS_REFERENCE

    def test_covers_entities(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Entities" in KERNOS_REFERENCE

    def test_covers_compaction(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Compaction" in KERNOS_REFERENCE

    def test_covers_covenant_rules(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Covenant Rules" in KERNOS_REFERENCE

    def test_covers_dispatch_gate(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Dispatch Gate" in KERNOS_REFERENCE

    def test_covers_proactive_awareness(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Proactive Awareness" in KERNOS_REFERENCE

    def test_covers_files(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "## Files" in KERNOS_REFERENCE

    def test_covers_mcp_tools(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "MCP Tools" in KERNOS_REFERENCE

    def test_covers_event_stream(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Event Stream" in KERNOS_REFERENCE

    def test_covers_web_browser(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Web Browser" in KERNOS_REFERENCE

    def test_covers_read_source(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "read_source" in KERNOS_REFERENCE

    def test_covers_retrieval(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "Retrieval" in KERNOS_REFERENCE or "Remember" in KERNOS_REFERENCE

    def test_kernel_tools_table(self):
        from kernos.messages.reference import KERNOS_REFERENCE
        assert "dismiss_whisper" in KERNOS_REFERENCE
        assert "remember" in KERNOS_REFERENCE
        assert "write_file" in KERNOS_REFERENCE


class TestReferenceProvisioning:
    """Test that kernos-reference.md is written during system space provisioning."""

    async def test_write_system_docs_includes_reference(self, tmp_path):
        """Verify _write_system_docs writes kernos-reference.md."""
        from kernos.kernel.files import FileService
        from kernos.capability.registry import CapabilityRegistry

        files = FileService(str(tmp_path))
        registry = CapabilityRegistry()

        handler = MagicMock()
        handler._files = files
        handler.registry = registry

        # Bind the real method
        from kernos.messages.handler import MessageHandler
        handler._write_system_docs = MessageHandler._write_system_docs.__get__(handler)
        handler._write_capabilities_overview = MessageHandler._write_capabilities_overview.__get__(handler)

        await handler._write_system_docs("test_tenant", "space_sys_test")

        # Check all three docs exist
        files_dir = tmp_path / "test_tenant" / "spaces" / "space_sys_test" / "files"
        assert (files_dir / "how-to-connect-tools.md").exists()
        assert (files_dir / "kernos-reference.md").exists()

        ref_content = (files_dir / "kernos-reference.md").read_text()
        assert "Kernos Architecture Reference" in ref_content
        assert "Context Spaces" in ref_content
        assert "read_source" in ref_content
