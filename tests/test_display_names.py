"""Tests for SURFACE-DISCIPLINE-PASS display-name resolver + sanitizers."""
import pytest

from kernos.kernel.display_names import (
    contains_internal_identifier, display_name_for_member,
    display_name_for_space, redact_internal_identifiers,
    strip_system_markers,
)


class _FakeInstanceDB:
    def __init__(self, profiles=None, members=None):
        self._profiles = profiles or {}
        self._members = members or {}

    async def get_member_profile(self, member_id):
        return self._profiles.get(member_id)

    async def get_member(self, member_id):
        return self._members.get(member_id)


class _FakeSpace:
    def __init__(self, name):
        self.name = name


class _FakeStateStore:
    def __init__(self, spaces=None):
        self._spaces = spaces or {}

    async def get_context_space(self, instance_id, space_id):
        return self._spaces.get((instance_id, space_id))


# ---- Resolver ----


@pytest.mark.asyncio
async def test_member_name_prefers_profile_display_name():
    idb = _FakeInstanceDB(
        profiles={"mem_abc": {"display_name": "Harold"}},
        members={"mem_abc": {"display_name": "IGNORED"}},
    )
    assert await display_name_for_member(idb, "mem_abc") == "Harold"


@pytest.mark.asyncio
async def test_member_name_falls_back_to_members_table():
    idb = _FakeInstanceDB(
        profiles={"mem_abc": {}},
        members={"mem_abc": {"display_name": "HaroldFallback"}},
    )
    assert await display_name_for_member(idb, "mem_abc") == "HaroldFallback"


@pytest.mark.asyncio
async def test_member_name_falls_back_to_id_when_unknown():
    idb = _FakeInstanceDB()
    assert await display_name_for_member(idb, "mem_unknown") == "mem_unknown"


@pytest.mark.asyncio
async def test_member_name_handles_none_db():
    assert await display_name_for_member(None, "mem_abc") == "mem_abc"


@pytest.mark.asyncio
async def test_space_name_resolves():
    state = _FakeStateStore({("inst1", "space_xx"): _FakeSpace("General")})
    assert await display_name_for_space(state, "inst1", "space_xx") == "General"


@pytest.mark.asyncio
async def test_space_name_falls_back():
    state = _FakeStateStore()
    assert await display_name_for_space(state, "inst1", "space_xx") == "space_xx"


# ---- Internal-id detection + redaction ----


def test_contains_internal_identifier_detects_mem():
    assert contains_internal_identifier("talk to mem_abc123def456 today") is True


def test_contains_internal_identifier_detects_space():
    assert contains_internal_identifier("go to space_deadbeef") is True


def test_contains_internal_identifier_ignores_prose():
    assert contains_internal_identifier("remember the mem card") is False
    assert contains_internal_identifier("outer space is cold") is False


def test_redact_internal_identifiers_replaces_both():
    text = "Harold (mem_abc123def) is in space_cafe1234."
    out = redact_internal_identifiers(text)
    assert "mem_abc123def" not in out
    assert "space_cafe1234" not in out
    assert "[internal-id-redacted]" in out


# ---- System-marker stripping ----


def test_strip_system_markers_basic():
    text = "[SYSTEM] The trigger fired.\nExtra detail."
    assert strip_system_markers(text) == "The trigger fired.\nExtra detail."


def test_strip_system_markers_colon_variant():
    text = "[SYSTEM: reminder fired] Pay the invoice today."
    assert strip_system_markers(text) == "Pay the invoice today."


def test_strip_system_markers_no_marker_unchanged():
    text = "Just a normal reply."
    assert strip_system_markers(text) == text


def test_strip_system_markers_handles_empty():
    assert strip_system_markers("") == ""
    assert strip_system_markers(None) is None
