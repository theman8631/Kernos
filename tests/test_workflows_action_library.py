"""Tests for the workflow action library.

WORKFLOW-LOOP-PRIMITIVE C4. Pins:

  - Each verb returns an ActionResult with the contracted shape.
  - World-effect verbs are covenant-gated; denied gates short-circuit.
  - route_to_agent fails LOUDLY with AgentInboxUnavailable when no
    provider is bound (provider-configuration-containment).
  - Direct-effect verbs (mark_state, append_to_ledger) use
    structural assertions; no LLM verifier.
  - Notion-independence: action_library.py source contains no Notion
    references; the AgentInbox Protocol stays provider-neutral.
  - ActionLibrary registry rejects duplicate registration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
    AppendToLedgerAction,
    CallToolAction,
    MarkStateAction,
    NotifyUserAction,
    PostToServiceAction,
    RouteToAgentAction,
    WriteCanvasAction,
)
from kernos.kernel.workflows.agent_inbox import (
    AgentInboxUnavailable,
    InMemoryAgentInbox,
)


@dataclass
class _Ctx:
    """Stand-in for a CohortContext that the engine will eventually
    construct. Just carries the fields the verbs read."""
    instance_id: str = "inst_a"
    member_id: str = "mem_a"


# ===========================================================================
# notify_user
# ===========================================================================


class TestNotifyUser:
    async def test_executes_and_verifies(self):
        delivered: list = []
        async def deliver(*, channel, message, urgency, instance_id, member_id):
            delivered.append((channel, message, urgency))
            return {"persisted_id": "msg-1"}
        verb = NotifyUserAction(deliver_fn=deliver)
        result = await verb.execute(_Ctx(), {"channel": "primary",
                                             "message": "hi",
                                             "urgency": "low"})
        assert result.success
        assert result.value == {"persisted_id": "msg-1"}
        assert delivered == [("primary", "hi", "low")]
        assert await verb.verify(_Ctx(), {"channel": "primary"}, result) is True

    async def test_covenant_denied_short_circuits(self):
        delivered: list = []
        async def deliver(**kw):
            delivered.append(kw)
            return {}
        async def gate(ctx, action_type, params):
            return False
        verb = NotifyUserAction(deliver_fn=deliver, covenant_gate=gate)
        result = await verb.execute(_Ctx(), {"channel": "x", "message": "x"})
        assert result.success is False
        assert result.error == "covenant_denied"
        assert delivered == []  # deliver_fn never called

    async def test_missing_param_fails_cleanly(self):
        async def deliver(**kw): return {}
        verb = NotifyUserAction(deliver_fn=deliver)
        result = await verb.execute(_Ctx(), {})  # no channel
        assert not result.success
        assert "missing_param" in (result.error or "")


# ===========================================================================
# write_canvas
# ===========================================================================


class TestWriteCanvas:
    async def test_replace_mode_verifies_exact_match(self):
        store = {"c1": ""}
        async def write(*, canvas_id, content, mode, instance_id):
            if mode == "replace":
                store[canvas_id] = content
            else:
                store[canvas_id] = (store.get(canvas_id, "") + content)
        async def read(*, canvas_id, instance_id):
            return store.get(canvas_id, "")
        verb = WriteCanvasAction(canvas_write_fn=write, canvas_read_fn=read)
        result = await verb.execute(_Ctx(), {
            "canvas_id": "c1",
            "content": "hello",
            "append_or_replace": "replace",
        })
        assert result.success
        assert await verb.verify(_Ctx(), {
            "canvas_id": "c1", "content": "hello", "append_or_replace": "replace",
        }, result) is True

    async def test_append_mode_verifies_substring(self):
        store = {"c1": "existing\n"}
        async def write(*, canvas_id, content, mode, instance_id):
            store[canvas_id] = store.get(canvas_id, "") + content
        async def read(*, canvas_id, instance_id):
            return store.get(canvas_id, "")
        verb = WriteCanvasAction(canvas_write_fn=write, canvas_read_fn=read)
        result = await verb.execute(_Ctx(), {
            "canvas_id": "c1",
            "content": "added",
            "append_or_replace": "append",
        })
        assert result.success
        assert await verb.verify(_Ctx(), {
            "canvas_id": "c1", "content": "added", "append_or_replace": "append",
        }, result) is True

    async def test_write_failure_propagates_clean_error(self):
        async def write(**kw): raise RuntimeError("disk on fire")
        async def read(**kw): return ""
        verb = WriteCanvasAction(canvas_write_fn=write, canvas_read_fn=read)
        result = await verb.execute(_Ctx(), {
            "canvas_id": "c1", "content": "x",
        })
        assert not result.success
        assert "disk on fire" in (result.error or "")


# ===========================================================================
# route_to_agent
# ===========================================================================


class TestRouteToAgent:
    async def test_no_provider_raises_loudly(self):
        verb = RouteToAgentAction(inbox=None)
        with pytest.raises(AgentInboxUnavailable):
            await verb.execute(_Ctx(), {"agent_id": "a1", "payload": {"x": 1}})

    async def test_in_memory_inbox_round_trip(self):
        inbox = InMemoryAgentInbox()
        verb = RouteToAgentAction(inbox=inbox)
        result = await verb.execute(_Ctx(), {
            "agent_id": "agent-claude",
            "payload": {"task": "review"},
        })
        assert result.success
        assert result.receipt["persisted_id"]
        ok = await verb.verify(_Ctx(), {
            "agent_id": "agent-claude",
            "payload": {"task": "review"},
        }, result)
        assert ok is True

    async def test_covenant_denied_short_circuits(self):
        inbox = InMemoryAgentInbox()
        async def gate(ctx, at, params): return False
        verb = RouteToAgentAction(inbox=inbox, covenant_gate=gate)
        result = await verb.execute(_Ctx(), {
            "agent_id": "a", "payload": {},
        })
        assert not result.success
        assert result.error == "covenant_denied"
        # No items posted.
        assert await inbox.read(agent_id="a", instance_id="inst_a") == []

    async def test_multi_tenant_isolation(self):
        """An item posted under inst_a is not visible to a read keyed
        to inst_b."""
        inbox = InMemoryAgentInbox()
        verb = RouteToAgentAction(inbox=inbox)
        await verb.execute(_Ctx(instance_id="inst_a"), {
            "agent_id": "a", "payload": {"x": 1},
        })
        items_b = await inbox.read(agent_id="a", instance_id="inst_b")
        assert items_b == []
        items_a = await inbox.read(agent_id="a", instance_id="inst_a")
        assert len(items_a) == 1


# ===========================================================================
# call_tool
# ===========================================================================


class TestCallTool:
    async def test_dispatch_and_default_verifier(self):
        async def dispatch(*, tool_id, args, instance_id, member_id):
            return {"ok": True, "echoed": args}
        verb = CallToolAction(tool_dispatch_fn=dispatch)
        result = await verb.execute(_Ctx(), {
            "tool_id": "echo", "args": {"v": 1},
        })
        assert result.success
        assert result.value == {"ok": True, "echoed": {"v": 1}}
        # No tool-specific verifier configured — default is success-bit.
        assert await verb.verify(_Ctx(), {"tool_id": "echo"}, result) is True

    async def test_tool_specific_verifier_consulted(self):
        async def dispatch(**kw): return {"ok": True}
        async def tool_verifier(*, tool_id, args, value, context):
            return value.get("ok") is True
        verb = CallToolAction(
            tool_dispatch_fn=dispatch, tool_verifier_fn=tool_verifier,
        )
        result = await verb.execute(_Ctx(), {"tool_id": "x"})
        assert await verb.verify(_Ctx(), {"tool_id": "x"}, result) is True


# ===========================================================================
# post_to_service
# ===========================================================================


class TestPostToService:
    async def test_post_round_trip(self):
        posted: list = []
        async def post(*, service_id, payload, instance_id):
            posted.append((service_id, payload))
            return {"id": "p-1"}
        verb = PostToServiceAction(service_post_fn=post)
        result = await verb.execute(_Ctx(), {
            "service_id": "discord",
            "payload": {"text": "hi"},
        })
        assert result.success
        assert posted == [("discord", {"text": "hi"})]


# ===========================================================================
# Direct-effect verbs
# ===========================================================================


class TestMarkState:
    async def test_set_then_verify_reads_new_value(self):
        store: dict = {}
        async def set_(*, key, value, scope, instance_id):
            store[(scope, instance_id, key)] = value
        async def get_(*, key, scope, instance_id):
            return store.get((scope, instance_id, key))
        verb = MarkStateAction(state_store_set=set_, state_store_get=get_)
        result = await verb.execute(_Ctx(), {
            "key": "k1", "value": 42, "scope": "instance",
        })
        assert result.success
        assert await verb.verify(_Ctx(), {
            "key": "k1", "value": 42, "scope": "instance",
        }, result) is True

    async def test_no_covenant_gate_for_direct_effect_verb(self):
        """Direct-effect verbs are NOT covenant-gated. Confirm
        construction takes no covenant_gate parameter."""
        import inspect
        sig = inspect.signature(MarkStateAction.__init__)
        assert "covenant_gate" not in sig.parameters


class TestAppendToLedger:
    async def test_append_then_verify_reads_last_entry(self):
        ledger: dict = {}
        async def append(*, workflow_id, entry, instance_id):
            ledger.setdefault((instance_id, workflow_id), []).append(entry)
        async def read_last(*, workflow_id, instance_id):
            entries = ledger.get((instance_id, workflow_id))
            return entries[-1] if entries else None
        verb = AppendToLedgerAction(
            ledger_append_fn=append, ledger_read_last_fn=read_last,
        )
        entry = {"step": 1, "synopsis": "did the thing"}
        result = await verb.execute(_Ctx(), {
            "workflow_id": "wf-1", "entry": entry,
        })
        assert result.success
        assert await verb.verify(_Ctx(), {
            "workflow_id": "wf-1", "entry": entry,
        }, result) is True


# ===========================================================================
# ActionLibrary registry
# ===========================================================================


class TestActionLibrary:
    async def test_register_and_get(self):
        async def deliver(**kw): return {}
        lib = ActionLibrary()
        verb = NotifyUserAction(deliver_fn=deliver)
        lib.register(verb)
        assert lib.has("notify_user")
        assert lib.get("notify_user") is verb

    async def test_register_duplicate_rejected(self):
        async def deliver(**kw): return {}
        lib = ActionLibrary()
        lib.register(NotifyUserAction(deliver_fn=deliver))
        with pytest.raises(ValueError, match="already registered"):
            lib.register(NotifyUserAction(deliver_fn=deliver))

    async def test_get_unknown_raises(self):
        lib = ActionLibrary()
        with pytest.raises(KeyError):
            lib.get("notify_user")


# ===========================================================================
# Notion-independence pin (structural)
# ===========================================================================


class TestNotionIndependence:
    """Spec invariant: route_to_agent goes through the AgentInbox
    Protocol. action_library.py MUST NOT reference Notion. Only
    NotionAgentInbox (in agent_inbox.py) may carry Notion specifics."""

    def test_action_library_has_no_notion_reference(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "kernos" / "kernel" / "workflows" / "action_library.py"
        )
        text = path.read_text()
        # Case-insensitive pin: if anyone adds "Notion" / "notion.so" /
        # "notion.com" / "notion-tool" anywhere in this file, fail.
        assert not re.search(r"\bnotion\b", text, re.IGNORECASE), (
            f"action_library.py contains a Notion reference; route_to_agent "
            f"must go through the AgentInbox Protocol only."
        )

    def test_agent_inbox_protocol_module_stays_provider_neutral(self):
        """The AgentInbox class definition section MUST stay
        provider-neutral. NotionAgentInbox is the only place Notion
        names may appear in agent_inbox.py."""
        path = (
            Path(__file__).resolve().parents[1]
            / "kernos" / "kernel" / "workflows" / "agent_inbox.py"
        )
        text = path.read_text()
        # Find the start of the NotionAgentInbox class — anything
        # before that line must be Notion-free.
        marker = "class NotionAgentInbox"
        marker_idx = text.find(marker)
        assert marker_idx > 0, "NotionAgentInbox class not found in agent_inbox.py"
        prelude = text[:marker_idx]
        # Strip docstring (everything between first triple-quote pair)
        stripped = re.sub(r'"""[\s\S]*?"""', "", prelude)
        assert not re.search(r"\bnotion\b", stripped, re.IGNORECASE), (
            f"agent_inbox.py prelude (everything before NotionAgentInbox) "
            f"contains a Notion reference; the Protocol section must stay "
            f"provider-neutral."
        )
