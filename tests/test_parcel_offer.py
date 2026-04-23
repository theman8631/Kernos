"""Parcel Pillar 2 — parcel-offer RM envelope + Messenger hook.

Spec reference: SPEC-PARCEL-PRIMITIVE-V1, Pillar 2 expected behaviors 1-4.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.relational_dispatch import RelationalDispatcher
from kernos.kernel.relational_messaging import RelationalMessage
from kernos.kernel.state_json import JsonStateStore
from kernos.utils import _safe_name


INSTANCE = "inst_offer"


def _space(root: Path, instance_id: str, space_id: str) -> Path:
    return root / _safe_name(instance_id) / "spaces" / space_id / "files"


@pytest.fixture
async def env(tmp_path):
    state = JsonStateStore(str(tmp_path))
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    await idb.declare_relationship("alice", "bob", "full-access")
    yield state, idb
    await idb.close()


class TestParcelOfferEnvelope:
    """Expected-behavior #1, #4: envelope_type + parcel_id flow through dispatch."""

    async def test_parcel_offer_envelope_persists_with_marker(self, env):
        state, idb = env
        dispatcher = RelationalDispatcher(
            state=state, instance_db=idb,
            trace_emitter=lambda *_a: None,
        )
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="alice", origin_agent_identity="Slate",
            addressee="bob", intent="inform",
            content="Parcel offer: 1 file, 5 bytes, note: test",
            envelope_type="parcel_offer",
            parcel_id="parcel_abc123",
        )
        assert result.ok is True
        assert result.messenger_decision == "none"  # no callback wired
        assert not result.envelope_skipped

        msg = await state.get_relational_message(INSTANCE, result.message_id)
        assert msg.envelope_type == "parcel_offer"
        assert msg.parcel_id == "parcel_abc123"

    async def test_default_envelope_type_is_message(self, env):
        state, idb = env
        dispatcher = RelationalDispatcher(
            state=state, instance_db=idb,
            trace_emitter=lambda *_a: None,
        )
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="alice", origin_agent_identity="Slate",
            addressee="bob", intent="inform",
            content="plain message",
        )
        assert result.ok is True
        msg = await state.get_relational_message(INSTANCE, result.message_id)
        assert msg.envelope_type == "message"
        assert msg.parcel_id == ""


class TestMessengerHookOnParcelOffer:
    """Expected-behavior #2, #3: Messenger runs on offers; refer auto-skips delivery."""

    async def test_messenger_pass_delivers_offer(self, env):
        state, idb = env

        async def _fake_messenger(
            *, instance_id, origin_member_id, addressee_member_id,
            intent, content,
        ):
            # pass outcome: return unchanged content, no whisper
            return content, None

        dispatcher = RelationalDispatcher(
            state=state, instance_db=idb,
            trace_emitter=lambda *_a: None,
            messenger_judge=_fake_messenger,
        )
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="alice", origin_agent_identity="Slate",
            addressee="bob", intent="inform",
            content="Parcel: 1 file",
            envelope_type="parcel_offer",
            parcel_id="parcel_xyz",
        )
        assert result.ok is True
        assert result.messenger_decision == "pass"
        assert not result.envelope_skipped
        assert result.message_id  # envelope persisted

    async def test_messenger_refer_skips_parcel_offer_delivery(self, env):
        state, idb = env

        # Minimal whisper stand-in — state.save_whisper only needs .whisper_id etc.
        from kernos.kernel.awareness import Whisper, generate_whisper_id
        from kernos.utils import utc_now

        def _make_whisper() -> Whisper:
            return Whisper(
                whisper_id=generate_whisper_id(),
                insight_text="refer: covenant conflict",
                delivery_class="ambient",
                source_space_id="",
                target_space_id="",
                supporting_evidence=[],
                reasoning_trace="Messenger referred",
                knowledge_entry_id="",
                foresight_signal="messenger:refer",
                created_at=utc_now(),
            )

        async def _fake_messenger(
            *, instance_id, origin_member_id, addressee_member_id,
            intent, content,
        ):
            return "referred; see whisper", _make_whisper()

        dispatcher = RelationalDispatcher(
            state=state, instance_db=idb,
            trace_emitter=lambda *_a: None,
            messenger_judge=_fake_messenger,
        )
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="alice", origin_agent_identity="Slate",
            addressee="bob", intent="inform",
            content="Parcel: 1 file",
            envelope_type="parcel_offer",
            parcel_id="parcel_refered",
        )
        assert result.ok is True
        assert result.messenger_decision == "refer"
        assert result.envelope_skipped is True
        assert result.message_id == ""  # no envelope created

        # No pending RM for the recipient
        pending = await state.query_relational_messages(
            instance_id=INSTANCE, addressee_member_id="bob", states=["pending"],
        )
        assert pending == []

    async def test_messenger_refer_on_plain_message_still_delivers(self, env):
        """Regression guard: refer on a plain (non-parcel) message still
        persists the envelope (with holding content) per existing RM behavior."""
        state, idb = env

        from kernos.kernel.awareness import Whisper, generate_whisper_id
        from kernos.utils import utc_now

        async def _fake_messenger(
            *, instance_id, origin_member_id, addressee_member_id,
            intent, content,
        ):
            wh = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text="hold", delivery_class="ambient",
                source_space_id="", target_space_id="",
                supporting_evidence=[], reasoning_trace="",
                knowledge_entry_id="", foresight_signal="",
                created_at=utc_now(),
            )
            return "holding response", wh

        dispatcher = RelationalDispatcher(
            state=state, instance_db=idb,
            trace_emitter=lambda *_a: None,
            messenger_judge=_fake_messenger,
        )
        result = await dispatcher.send(
            instance_id=INSTANCE,
            origin_member_id="alice", origin_agent_identity="Slate",
            addressee="bob", intent="inform",
            content="plain text",
            # default envelope_type=message
        )
        # Refer on plain message: still ok, envelope still created
        assert result.ok is True
        assert result.messenger_decision == "refer"
        assert result.envelope_skipped is False
        assert result.message_id  # envelope persisted

        msg = await state.get_relational_message(INSTANCE, result.message_id)
        assert msg.content == "holding response"
