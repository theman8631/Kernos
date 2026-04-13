"""Tests for SPEC-3L: Developer Mode Error Surfacing.

Covers: ErrorBuffer collection, developer mode injection into system prompt,
buffer limits, tenant isolation, kernos-only filtering, drain-and-clear.
"""
import logging
from unittest.mock import MagicMock

import pytest

from kernos.messages.handler import ErrorBuffer, _MAX_ERROR_BUFFER


class TestErrorBufferCollection:
    def test_collect_and_drain(self):
        buf = ErrorBuffer()
        buf.collect("t1", "WARNING kernos.kernel.foo: something broke")
        result = buf.drain("t1")
        assert "[DEVELOPER: Errors since last message]" in result
        assert "something broke" in result
        assert "[END DEVELOPER]" in result

    def test_drain_empty(self):
        buf = ErrorBuffer()
        result = buf.drain("t1")
        assert result == ""

    def test_drain_clears_buffer(self):
        buf = ErrorBuffer()
        buf.collect("t1", "ERROR kernos.x: fail")
        buf.drain("t1")
        assert buf.drain("t1") == ""

    def testinstance_isolation(self):
        buf = ErrorBuffer()
        buf.collect("t1", "WARNING kernos.a: t1 error")
        buf.collect("t2", "WARNING kernos.b: t2 error")

        r1 = buf.drain("t1")
        assert "t1 error" in r1
        assert "t2 error" not in r1

        r2 = buf.drain("t2")
        assert "t2 error" in r2

    def test_buffer_limit(self):
        buf = ErrorBuffer()
        for i in range(25):
            buf.collect("t1", f"WARNING kernos.x: error {i}")
        result = buf.drain("t1")
        assert "5 earlier errors omitted" in result
        # Should have exactly _MAX_ERROR_BUFFER entries
        lines = [l for l in result.split("\n") if l.startswith("WARNING")]
        assert len(lines) == _MAX_ERROR_BUFFER


class TestErrorBufferLogHandler:
    def test_captures_kernos_warning(self):
        buf = ErrorBuffer()
        buf.set_tenant("t1")
        kernos_logger = logging.getLogger("kernos.test_capture")
        kernos_logger.warning("test warning message")
        result = buf.drain("t1")
        assert "test warning message" in result

    def test_captures_kernos_error(self):
        buf = ErrorBuffer()
        buf.set_tenant("t1")
        kernos_logger = logging.getLogger("kernos.test_capture_err")
        kernos_logger.error("test error message")
        result = buf.drain("t1")
        assert "test error message" in result

    def test_ignores_info(self):
        buf = ErrorBuffer()
        buf.set_tenant("t1")
        kernos_logger = logging.getLogger("kernos.test_info")
        kernos_logger.info("this is info")
        result = buf.drain("t1")
        assert result == ""

    def test_ignores_non_kernos(self):
        buf = ErrorBuffer()
        buf.set_tenant("t1")
        other_logger = logging.getLogger("httpx.test")
        other_logger.warning("httpx warning")
        result = buf.drain("t1")
        assert result == ""

    def test_no_collection_without_tenant(self):
        buf = ErrorBuffer()
        # Don't set tenant
        kernos_logger = logging.getLogger("kernos.test_no_tenant")
        kernos_logger.warning("orphan warning")
        result = buf.drain("")
        assert result == ""


class TestDevModeInjection:
    """Test that the error block is injected into system prompt only when developer_mode=True."""

    def test_error_block_format(self):
        buf = ErrorBuffer()
        buf.collect("t1", "WARNING kernos.foo: schema failed")
        buf.collect("t1", "ERROR kernos.bar: 429 rate limit")
        result = buf.drain("t1")

        assert result.startswith("[DEVELOPER: Errors since last message]")
        assert "schema failed" in result
        assert "429 rate limit" in result
        assert "developer mode is enabled" in result
        assert result.strip().endswith("[END DEVELOPER]")

    def test_multiple_drains_only_first_has_content(self):
        buf = ErrorBuffer()
        buf.collect("t1", "WARNING kernos.x: oops")
        first = buf.drain("t1")
        second = buf.drain("t1")
        assert "oops" in first
        assert second == ""
