"""Friction Observer — detects system friction and writes diagnostic reports.

Post-turn cohort agent. Reads turn trace data, detects friction patterns,
writes self-contained bug reports to data/diagnostics/friction/.

Biased toward subtraction: REMOVE > STRUCTURAL_ENFORCE > SIMPLIFY > ADD.
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Preference-shaped language patterns (signal 7)
_PREF_PATTERNS = [
    re.compile(r"\b(from now on|always|never|every time|whenever|each time)\b", re.I),
    re.compile(r"\b(remind me|notify me|let me know|alert me)\b.{0,40}\b(before|when|if|every)\b", re.I),
    re.compile(r"\b(don'?t|do not|stop)\b.{0,30}\b(ask|confirm|check|send|notify)\b", re.I),
]


@dataclass
class FrictionSignal:
    """A detected friction event."""
    signal_type: str          # e.g. TOOL_REQUEST_FOR_SURFACED_TOOL
    description: str          # Human-readable short description
    evidence: list[str]       # Log-like evidence lines
    context: dict             # Snapshot: user message, space, tools, etc.
    heuristic: bool = False   # True for low-confidence signals (2, 4)


class FrictionObserver:
    """Detects system friction from turn trace data.

    Called post-turn with the TurnContext and tool trace. Produces
    FrictionSignal objects and writes reports to disk.
    """

    def __init__(
        self,
        reasoning: Any = None,
        data_dir: str = "./data",
        enabled: bool = True,
    ) -> None:
        self._reasoning = reasoning
        self._data_dir = data_dir
        self._enabled = enabled

    async def observe(
        self,
        *,
        tenant_id: str,
        user_message: str,
        response_text: str,
        tool_trace: list[dict],
        surfaced_tool_names: set[str],
        active_space_id: str,
        merged_count: int,
        is_reactive: bool,
        pref_detected: bool,
        provider_errors: list[str] | None = None,
    ) -> list[FrictionSignal]:
        """Run all signal detectors and write reports for any friction found.

        Returns the list of detected signals (empty if no friction).
        """
        if not self._enabled:
            return []

        signals: list[FrictionSignal] = []

        ctx_snapshot = {
            "user_message": user_message[:500],
            "space": active_space_id,
            "surfaced_tool_count": len(surfaced_tool_names),
            "tool_calls": [t["name"] for t in tool_trace],
            "merged_count": merged_count,
            "is_reactive": is_reactive,
        }

        # Signal 1: TOOL_REQUEST_FOR_SURFACED_TOOL
        sig = self._check_request_for_surfaced(tool_trace, surfaced_tool_names, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 3: GATE_CONFIRM_ON_REACTIVE
        sig = self._check_gate_confirm_reactive(tool_trace, is_reactive, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 5: SCHEMA_ERROR_ON_PROVIDER
        sig = self._check_schema_error(tool_trace, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 6: MERGED_MESSAGES_DROPPED
        sig = self._check_merged_dropped(merged_count, response_text, user_message, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 7: PREFERENCE_STATED_BUT_NOT_CAPTURED
        sig = self._check_pref_missed(user_message, pref_detected, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 2: STALE_DATA_IN_RESPONSE (heuristic)
        sig = self._check_stale_data(tool_trace, response_text, user_message, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 4: TOOL_AVAILABLE_BUT_NOT_USED (heuristic)
        sig = self._check_tool_not_used(
            tool_trace, surfaced_tool_names, user_message, response_text, ctx_snapshot
        )
        if sig:
            signals.append(sig)

        # Signal 8: PROVIDER_ERROR_REPEATED
        sig = self._check_provider_errors(provider_errors or [], ctx_snapshot)
        if sig:
            signals.append(sig)

        # Write reports
        for signal in signals:
            logger.warning(
                "FRICTION: type=%s desc=%s",
                signal.signal_type, signal.description[:120],
            )
            try:
                await self._write_report(signal, tenant_id)
            except Exception as exc:
                logger.warning("FRICTION: failed to write report: %s", exc)

        return signals

    # ------------------------------------------------------------------
    # Signal detectors
    # ------------------------------------------------------------------

    def _check_request_for_surfaced(
        self, tool_trace: list[dict], surfaced: set[str], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 1: Agent called request_tool when the target tool was already surfaced."""
        for call in tool_trace:
            if call["name"] != "request_tool":
                continue
            # Check if any surfaced tool matches what was requested
            cap_name = call.get("input", {}).get("capability_name", "")
            desc = call.get("input", {}).get("description", "").lower()

            # Check if the capability's tools are in the surfaced set
            matching_surfaced = []
            for tool_name in surfaced:
                if cap_name and cap_name != "unknown" and cap_name in tool_name:
                    matching_surfaced.append(tool_name)
                elif any(kw in tool_name.lower() for kw in desc.split()[:5] if len(kw) > 3):
                    matching_surfaced.append(tool_name)

            # Also check common tool categories
            _cal_tools = {"create-event", "list-events", "search-events", "get-event"}
            _search_tools = {"brave_web_search", "brave_local_search"}
            if any(kw in desc for kw in ["calendar", "event", "appointment", "schedule"]):
                matching_surfaced.extend(t for t in _cal_tools if t in surfaced)
            if any(kw in desc for kw in ["search", "web", "find", "look up"]):
                matching_surfaced.extend(t for t in _search_tools if t in surfaced)

            if matching_surfaced:
                return FrictionSignal(
                    signal_type="TOOL_REQUEST_FOR_SURFACED_TOOL",
                    description=(
                        f"Agent called request_tool (capability='{cap_name}') but "
                        f"matching tools already surfaced: {sorted(set(matching_surfaced))}"
                    ),
                    evidence=[
                        f"request_tool input: capability_name='{cap_name}', description='{desc[:100]}'",
                        f"Surfaced tools ({len(surfaced)}): {sorted(surfaced)[:10]}...",
                        f"Matching surfaced: {sorted(set(matching_surfaced))}",
                    ],
                    context=ctx,
                )
        return None

    def _check_gate_confirm_reactive(
        self, tool_trace: list[dict], is_reactive: bool, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 3: Gate returned CONFIRM for reactive soft_write — shouldn't happen."""
        # This is detected from gate events, not tool trace directly.
        # The gate bypass should prevent this entirely. If we see gate blocks
        # on reactive soft_write, the bypass isn't working.
        # For V1: detect if request_tool was called because a tool was blocked
        # (this is a proxy — the actual gate trace would need event stream reading)
        return None  # V1: rely on GATE log lines; full integration in V2

    def _check_schema_error(
        self, tool_trace: list[dict], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 5: Structured output schema error from provider."""
        # This fires from PREF_DETECT logs, not tool_trace.
        # V1: handled by checking pref_detected flag externally.
        return None  # Detected at the source (preference_parser logs)

    def _check_merged_dropped(
        self, merged_count: int, response: str, user_message: str, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 6: Agent addressed fewer topics than merged messages."""
        if merged_count <= 1:
            return None

        # Simple heuristic: count sentences in response vs merged count
        # A response with fewer than merged_count substantial paragraphs
        # is a potential drop signal
        response_paragraphs = [p.strip() for p in response.split("\n\n") if len(p.strip()) > 20]
        if len(response_paragraphs) >= merged_count:
            return None  # Addressed enough topics

        # Check if the response is very short relative to message count
        words = len(response.split())
        if words < merged_count * 10:  # Less than ~10 words per message
            return FrictionSignal(
                signal_type="MERGED_MESSAGES_DROPPED",
                description=(
                    f"Turn had {merged_count} merged messages but response "
                    f"only has {len(response_paragraphs)} paragraphs / {words} words"
                ),
                evidence=[
                    f"Merged count: {merged_count}",
                    f"Response paragraphs: {len(response_paragraphs)}",
                    f"Response words: {words}",
                ],
                context=ctx,
            )
        return None

    def _check_pref_missed(
        self, user_message: str, pref_detected: bool, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 7: User said something preference-shaped but no detection fired."""
        if pref_detected:
            return None  # Detection pipeline ran

        if any(p.search(user_message) for p in _PREF_PATTERNS):
            return FrictionSignal(
                signal_type="PREFERENCE_STATED_BUT_NOT_CAPTURED",
                description=(
                    f"User message contains preference-shaped language but "
                    f"no PREF_DETECT event fired"
                ),
                evidence=[
                    f"User message: {user_message[:200]}",
                    f"Matched pattern: preference-shaped language detected",
                    f"pref_detected: {pref_detected}",
                ],
                context=ctx,
            )
        return None

    def _check_stale_data(
        self, tool_trace: list[dict], response: str, user_message: str, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 2: Agent answered with data from context instead of calling a tool. Heuristic."""
        msg_lower = user_message.lower()

        # Time queries without get-current-time call
        time_keywords = ["what time", "current time", "time is it", "what's the time"]
        if any(kw in msg_lower for kw in time_keywords):
            tool_names = {t["name"] for t in tool_trace}
            if "get-current-time" not in tool_names:
                return FrictionSignal(
                    signal_type="STALE_DATA_IN_RESPONSE",
                    description="Agent answered a time query without calling get-current-time",
                    evidence=[
                        f"User asked about time: {user_message[:100]}",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                        f"get-current-time was NOT called",
                    ],
                    context=ctx,
                    heuristic=True,
                )
        return None

    def _check_tool_not_used(
        self, tool_trace: list[dict], surfaced: set[str],
        user_message: str, response: str, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 4: Agent had a tool for the query but didn't use it. Heuristic."""
        msg_lower = user_message.lower()
        tool_names = {t["name"] for t in tool_trace}

        # Preference/state queries without inspect_state
        state_keywords = ["what preferences", "what settings", "what's set up",
                         "what do i have set", "my notifications", "my triggers"]
        if any(kw in msg_lower for kw in state_keywords):
            if "inspect_state" in surfaced and "inspect_state" not in tool_names:
                return FrictionSignal(
                    signal_type="TOOL_AVAILABLE_BUT_NOT_USED",
                    description="Agent answered a state query without calling inspect_state",
                    evidence=[
                        f"User asked about state: {user_message[:100]}",
                        f"inspect_state: surfaced but NOT called",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                    ],
                    context=ctx,
                    heuristic=True,
                )

        # Schedule queries without manage_schedule
        # "my schedule" / "what's on my schedule" → calendar query, list-events is correct
        # "my reminders" / "what triggers" → trigger query, manage_schedule expected
        sched_keywords = ["what reminders", "my reminders", "what triggers"]
        if any(kw in msg_lower for kw in sched_keywords):
            # If agent used list-events, it answered a calendar query — not a trigger miss
            if "list-events" in tool_names:
                return None
            if "manage_schedule" in surfaced and "manage_schedule" not in tool_names:
                return FrictionSignal(
                    signal_type="TOOL_AVAILABLE_BUT_NOT_USED",
                    description="Agent answered a schedule query without calling manage_schedule",
                    evidence=[
                        f"User asked about schedule: {user_message[:100]}",
                        f"manage_schedule: surfaced but NOT called",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                    ],
                    context=ctx,
                    heuristic=True,
                )
        return None

    def _check_provider_errors(
        self, errors: list[str], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 8: Same provider error occurs 2+ times — infrastructure bug."""
        if len(errors) < 2:
            return None

        # Count error frequencies
        from collections import Counter
        counts = Counter(errors)
        repeated = {msg: count for msg, count in counts.items() if count >= 2}
        if not repeated:
            return None

        top_msg, top_count = max(repeated.items(), key=lambda x: x[1])
        return FrictionSignal(
            signal_type="PROVIDER_ERROR_REPEATED",
            description=(
                f"Provider error occurred {top_count}x in this session: "
                f"{top_msg[:100]}"
            ),
            evidence=[
                f"Error message: {top_msg[:200]}",
                f"Occurrences: {top_count}",
                f"Total errors this session: {len(errors)}",
                f"Distinct errors: {len(counts)}",
                "Check provider code for error parsing — if message is always "
                "'unknown', the parsing code may be reading the wrong field",
            ],
            context=ctx,
        )

    # ------------------------------------------------------------------
    # Report writing
    # ------------------------------------------------------------------

    async def _write_report(self, signal: FrictionSignal, tenant_id: str) -> None:
        """Write a friction report file with LLM-generated description."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_type = signal.signal_type.replace(" ", "_")[:40]
        filename = f"FRICTION_{ts}_{safe_type}.md"

        friction_dir = os.path.join(self._data_dir, "diagnostics", "friction")
        os.makedirs(friction_dir, exist_ok=True)
        filepath = os.path.join(friction_dir, filename)

        # Generate LLM description if reasoning service available
        llm_description = ""
        recommendation = "SIMPLIFY"
        if self._reasoning:
            try:
                llm_description, recommendation = await self._generate_description(signal)
            except Exception as exc:
                logger.warning("FRICTION: LLM description failed: %s", exc)
                llm_description = signal.description
                recommendation = self._default_recommendation(signal.signal_type)
        else:
            llm_description = signal.description
            recommendation = self._default_recommendation(signal.signal_type)

        heuristic_note = ""
        if signal.heuristic:
            heuristic_note = "\n**Confidence:** LOW (heuristic — may be a false positive)\n"

        report = (
            f"# Friction Report: {signal.signal_type}\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
            f"{heuristic_note}\n"
            f"## Description\n{llm_description}\n\n"
            f"## Recommendation: {recommendation}\n"
            f"{signal.description}\n\n"
            f"## Evidence\n"
        )
        for line in signal.evidence:
            report += f"- {line}\n"
        report += (
            f"\n## Context\n"
            f"User message: {signal.context.get('user_message', 'N/A')}\n"
            f"Space: {signal.context.get('space', 'N/A')}\n"
            f"Tools surfaced: {signal.context.get('surfaced_tool_count', 'N/A')}\n"
            f"Tool calls: {signal.context.get('tool_calls', [])}\n"
            f"Merged count: {signal.context.get('merged_count', 0)}\n"
            f"Reactive: {signal.context.get('is_reactive', True)}\n"
        )

        with open(filepath, "w") as f:
            f.write(report)

        logger.info("FRICTION_REPORT: written %s", filepath)

    async def _generate_description(self, signal: FrictionSignal) -> tuple[str, str]:
        """Use a cheap LLM call to generate a human-readable friction description."""
        prompt = (
            "You are a system diagnostics observer. A friction event was detected "
            "in an AI assistant system. Write a clear, concise description of what "
            "went wrong, why it matters, and what the likely fix is.\n\n"
            "Your recommendation MUST be one of:\n"
            "- REMOVE — delete the thing causing friction\n"
            "- STRUCTURAL_ENFORCE — enforce in code, not prompt\n"
            "- SIMPLIFY — make the existing mechanism cleaner\n"
            "- ADD — add something new (last resort)\n\n"
            "Format: first line is the recommendation word, rest is description."
        )
        evidence_text = "\n".join(f"- {e}" for e in signal.evidence)
        user_content = (
            f"Signal: {signal.signal_type}\n"
            f"Summary: {signal.description}\n"
            f"Evidence:\n{evidence_text}\n"
            f"Context: {json.dumps(signal.context, default=str)[:500]}"
        )

        raw = await self._reasoning.complete_simple(
            system_prompt=prompt,
            user_content=user_content,
            max_tokens=300,
            prefer_cheap=True,
        )

        lines = raw.strip().split("\n", 1)
        first = lines[0].strip().upper()
        desc = lines[1].strip() if len(lines) > 1 else signal.description

        valid_recs = {"REMOVE", "STRUCTURAL_ENFORCE", "SIMPLIFY", "ADD"}
        recommendation = first if first in valid_recs else self._default_recommendation(signal.signal_type)

        return desc, recommendation

    @staticmethod
    def _default_recommendation(signal_type: str) -> str:
        """Default recommendation based on signal type."""
        defaults = {
            "TOOL_REQUEST_FOR_SURFACED_TOOL": "SIMPLIFY",
            "STALE_DATA_IN_RESPONSE": "SIMPLIFY",
            "GATE_CONFIRM_ON_REACTIVE": "STRUCTURAL_ENFORCE",
            "TOOL_AVAILABLE_BUT_NOT_USED": "SIMPLIFY",
            "SCHEMA_ERROR_ON_PROVIDER": "STRUCTURAL_ENFORCE",
            "MERGED_MESSAGES_DROPPED": "SIMPLIFY",
            "PREFERENCE_STATED_BUT_NOT_CAPTURED": "SIMPLIFY",
            "PROVIDER_ERROR_REPEATED": "STRUCTURAL_ENFORCE",
        }
        return defaults.get(signal_type, "SIMPLIFY")
