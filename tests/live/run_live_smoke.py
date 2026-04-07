#!/usr/bin/env python3
"""Live smoke test — validates core Kernos behavior against the real provider.

Usage: source .venv/bin/activate && python tests/live/run_live_smoke.py

Requires: OPENAI_CODEX credentials in .credentials/openai-codex.json
          (or ANTHROPIC_API_KEY for Anthropic provider)

Outputs: tests/live/SMOKE_RESULTS.md with pass/fail for each test.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("live_smoke")

# Capture log lines for analysis
_captured_logs: list[str] = []
_root_handler = logging.StreamHandler()
_root_handler.setLevel(logging.INFO)

class LogCapture(logging.Handler):
    def emit(self, record):
        _captured_logs.append(self.format(record))

_capture_handler = LogCapture()
_capture_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_capture_handler)


async def build_handler():
    """Build a MessageHandler with real provider, same as server.py."""
    from dotenv import load_dotenv
    load_dotenv()

    from kernos.kernel.state_json import JsonStateStore
    from kernos.kernel.events import JsonEventStream
    from kernos.persistence.json_file import JsonConversationStore, JsonTenantStore, JsonAuditStore
    from kernos.capability.client import MCPClientManager
    from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.engine import TaskEngine
    from kernos.messages.handler import MessageHandler
    import dataclasses

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    state = JsonStateStore(data_dir)
    events = JsonEventStream(data_dir)
    conversations = JsonConversationStore(data_dir)
    tenants = JsonTenantStore(data_dir)
    audit = JsonAuditStore(data_dir)

    # MCP — skip connection for smoke tests (too slow, requires servers running)
    mcp = MCPClientManager(events)

    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    provider_name = os.getenv("KERNOS_LLM_PROVIDER", "openai-codex")
    if provider_name == "openai-codex":
        from kernos.kernel.credentials import resolve_openai_codex_credential
        from kernos.providers.codex_provider import OpenAICodexProvider
        provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    else:
        from kernos.providers.anthropic_provider import AnthropicProvider, resolve_anthropic_credential
        provider = AnthropicProvider(api_key=resolve_anthropic_credential())

    reasoning = ReasoningService(provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
    handler.register_mcp_tools_in_catalog()
    return handler


def make_message(content: str, tenant_id: str = "live_smoke_test"):
    from kernos.messages.models import NormalizedMessage, AuthLevel
    return NormalizedMessage(
        content=content,
        sender="smoke_tester",
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id="smoke_conv",
        timestamp=datetime.now(timezone.utc),
        tenant_id=tenant_id,
    )


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.response = ""
        self.error = ""
        self.duration_ms = 0
        self.notes: list[str] = []

    def to_md(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"### {status}: {self.name}"]
        if self.duration_ms:
            lines.append(f"Duration: {self.duration_ms}ms")
        if self.response:
            lines.append(f"Response: {self.response[:200]}")
        if self.error:
            lines.append(f"Error: {self.error}")
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


async def run_test(handler, name: str, message: str, checks: list) -> TestResult:
    """Run a single test: send message, validate response."""
    result = TestResult(name)
    _captured_logs.clear()
    t0 = time.monotonic()
    try:
        response = await handler.process(make_message(message))
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        result.response = response or ""

        # Run checks
        all_passed = True
        for check_name, check_fn in checks:
            try:
                passed = check_fn(response, _captured_logs)
                if passed:
                    result.notes.append(f"OK: {check_name}")
                else:
                    result.notes.append(f"FAIL: {check_name}")
                    all_passed = False
            except Exception as exc:
                result.notes.append(f"FAIL: {check_name} — {exc}")
                all_passed = False

        result.passed = all_passed
    except Exception as exc:
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        result.error = str(exc)
    return result


def check_nonempty(resp, logs):
    return bool(resp and resp.strip())

def check_no_error(resp, logs):
    return "went wrong" not in (resp or "").lower()

def check_contains(text):
    def _check(resp, logs):
        return text.lower() in (resp or "").lower()
    return _check

def check_log_contains(pattern):
    def _check(resp, logs):
        return any(pattern in line for line in logs)
    return _check


async def main():
    logger.info("Building handler with real provider...")
    handler = await build_handler()
    logger.info("Handler ready. Running smoke tests...\n")

    results: list[TestResult] = []

    # Test 1: Basic response
    r = await run_test(handler, "Basic response", "What time is it?", [
        ("Response not empty", check_nonempty),
        ("No error message", check_no_error),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Test 2: Context dump (structural confidence check)
    r = await run_test(handler, "/dump check", "/dump", [
        ("Response mentions dump", check_contains("dump")),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Test 3: Structured output (router should work)
    r = await run_test(handler, "Router works", "Hello, how are you?", [
        ("Response not empty", check_nonempty),
        ("No error message", check_no_error),
        ("Router fired", check_log_contains("ROUTE:")),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Test 4: Knowledge shaping works
    r = await run_test(handler, "Knowledge shaping", "What do you know about me?", [
        ("Response not empty", check_nonempty),
        ("No error message", check_no_error),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Test 5: Tool surfacing
    r = await run_test(handler, "Tool surfacing", "Search for pizza places near me", [
        ("Response not empty", check_nonempty),
        ("Tool surfacing logged", check_log_contains("TOOL_SURFACING:")),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Test 6: Computation via execute_code
    r = await run_test(handler, "Code execution", "What is 2 to the power of 100? Use execute_code to compute it.", [
        ("Response not empty", check_nonempty),
        ("No error message", check_no_error),
    ])
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({r.duration_ms}ms)")

    # Write results
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = f"# Live Smoke Test Results\n\n"
    md += f"**Date:** {ts}\n"
    md += f"**Result:** {passed}/{total} passed\n"
    md += f"**Provider:** {os.getenv('KERNOS_LLM_PROVIDER', 'openai-codex')}\n\n"
    for r in results:
        md += r.to_md() + "\n\n"

    output_path = Path("tests/live/SMOKE_RESULTS.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md)
    logger.info(f"\n{'='*50}")
    logger.info(f"SMOKE TEST: {passed}/{total} passed")
    logger.info(f"Results written to {output_path}")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
