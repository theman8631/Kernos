"""Tests for the kernos setup first-run flow.

Covers Section 5 of the INSTALL-FOR-STOCK-CONNECTORS spec:
TTY-vs-headless behavior, --non-interactive / --enable-services /
--all-enabled flags, daemon-never-blocks invariant, idempotent
re-run on existing state.
"""

from __future__ import annotations

import argparse

import pytest

from kernos.setup.first_run import (
    FirstRunResult,
    FirstRunSetupError,
    is_interactive,
    run_first_run,
)
from kernos.setup.service_state import (
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scripted_input(answers: list[str]):
    """Build an input_fn that returns each answer in turn."""
    pending = list(answers)

    def _fn(prompt: str = "") -> str:
        if not pending:
            raise EOFError("scripted input exhausted")
        return pending.pop(0)

    return _fn


def _capture_print():
    captured: list[str] = []

    def _fn(*args) -> None:
        captured.append(" ".join(str(a) for a in args))

    return captured, _fn


# ---------------------------------------------------------------------------
# is_interactive
# ---------------------------------------------------------------------------


def test_is_interactive_false_when_either_stream_isnt_tty():
    class _NotTTY:
        def isatty(self):
            return False

    class _TTY:
        def isatty(self):
            return True

    assert is_interactive(stdin=_NotTTY(), stdout=_TTY()) is False
    assert is_interactive(stdin=_TTY(), stdout=_NotTTY()) is False
    assert is_interactive(stdin=_NotTTY(), stdout=_NotTTY()) is False


def test_is_interactive_true_when_both_streams_are_tty():
    class _TTY:
        def isatty(self):
            return True

    assert is_interactive(stdin=_TTY(), stdout=_TTY()) is True


def test_is_interactive_false_on_isatty_exception():
    class _Bad:
        def isatty(self):
            raise OSError("nope")

    assert is_interactive(stdin=_Bad(), stdout=_Bad()) is False


# ---------------------------------------------------------------------------
# Headless bootstrap (non-TTY, no flags)
# ---------------------------------------------------------------------------


def test_headless_bootstrap_writes_all_disabled_state(tmp_path):
    captured, print_fn = _capture_print()
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=False,
        print_fn=print_fn,
    )
    assert result.bootstrapped_disabled is True
    assert result.ran_interactive is False
    assert result.skipped_existing_state is False

    store = ServiceStateStore(tmp_path)
    states = store.list_all()
    assert all(not s.enabled for s in states)
    assert all(s.source is ServiceStateSource.SETUP for s in states)
    assert all(
        s.updated_by is ServiceStateUpdatedBy.SYSTEM for s in states
    )
    # The instructions land in the print buffer.
    joined = "\n".join(captured)
    assert "kernos services enable" in joined
    assert "kernos setup" in joined


def test_headless_bootstrap_skips_when_state_already_exists(tmp_path):
    """Daemon never blocks: existing state preserved; flow exits clean."""
    pre_existing = ServiceStateStore(tmp_path)
    pre_existing.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    captured, print_fn = _capture_print()
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=False,
        print_fn=print_fn,
    )
    assert result.skipped_existing_state is True
    assert result.bootstrapped_disabled is False
    # Pre-existing state untouched.
    assert ServiceStateStore(tmp_path).is_enabled("notion") is True


# ---------------------------------------------------------------------------
# Non-interactive flags
# ---------------------------------------------------------------------------


def test_non_interactive_with_enable_services_writes_chosen(tmp_path):
    captured, print_fn = _capture_print()
    result = run_first_run(
        data_dir=tmp_path,
        non_interactive=True,
        enable_services=["notion"],
        interactive_override=True,  # explicit flags trump TTY
        print_fn=print_fn,
    )
    assert result.bootstrapped_disabled is False
    assert result.ran_interactive is False
    assert "notion" in result.enabled_service_ids
    # google_drive (the other shipped service) ends up disabled.
    assert "google_drive" in result.disabled_service_ids
    store = ServiceStateStore(tmp_path)
    assert store.is_enabled("notion") is True
    assert store.is_enabled("google_drive") is False


def test_all_enabled_flag_enables_every_shipped_service(tmp_path):
    _captured, print_fn = _capture_print()
    result = run_first_run(
        data_dir=tmp_path,
        all_enabled=True,
        interactive_override=True,
        print_fn=print_fn,
    )
    store = ServiceStateStore(tmp_path)
    assert store.is_enabled("notion") is True
    assert store.is_enabled("google_drive") is True
    assert result.disabled_service_ids == ()


def test_non_interactive_unknown_service_raises_with_friendly_error(tmp_path):
    _captured, print_fn = _capture_print()
    with pytest.raises(FirstRunSetupError, match="unknown"):
        run_first_run(
            data_dir=tmp_path,
            non_interactive=True,
            enable_services=["nonexistent"],
            interactive_override=False,
            print_fn=print_fn,
        )
    # Failed setup leaves no partial state.
    assert not ServiceStateStore(tmp_path).exists()


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------


def test_interactive_prompt_persists_yes_answers(tmp_path):
    captured, print_fn = _capture_print()
    # Order matches alphabetical service iteration: google_drive, notion.
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_scripted_input(["y", "n"]),
        print_fn=print_fn,
    )
    assert result.ran_interactive is True
    assert "google_drive" in result.enabled_service_ids
    assert "notion" in result.disabled_service_ids


def test_interactive_prompt_default_when_user_presses_enter(tmp_path):
    """Empty answer accepts the suggested default. For a fresh install
    (no prior state), the default for every service is 'no' (disabled)."""
    captured, print_fn = _capture_print()
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_scripted_input(["", ""]),
        print_fn=print_fn,
    )
    # All services accepted defaults → disabled.
    assert result.enabled_service_ids == ()
    assert "notion" in result.disabled_service_ids
    assert "google_drive" in result.disabled_service_ids


def test_interactive_prompt_existing_state_uses_prior_value_as_default(tmp_path):
    """Re-run on existing state suggests prior value as default."""
    # Pre-flip notion to enabled.
    ServiceStateStore(tmp_path).set(
        "notion",
        enabled=True,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    _captured, print_fn = _capture_print()
    # Empty answers preserve defaults (notion: True, google_drive: False).
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_scripted_input(["", ""]),
        print_fn=print_fn,
    )
    assert "notion" in result.enabled_service_ids
    assert "google_drive" in result.disabled_service_ids


def test_interactive_prompt_handles_eof_without_writing_state(tmp_path):
    """Ctrl-D mid-prompt aborts cleanly; no state written."""
    captured, print_fn = _capture_print()

    def _eof_fn(prompt: str = "") -> str:
        raise EOFError()

    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_eof_fn,
        print_fn=print_fn,
    )
    assert result.ran_interactive is True
    assert not ServiceStateStore(tmp_path).exists()
    joined = "\n".join(captured)
    assert "aborted" in joined.lower()


def test_interactive_prompt_unrecognized_input_falls_through_to_default(tmp_path):
    captured, print_fn = _capture_print()
    # First service: 'maybe' (unrecognized) → treated as default (False
    # for fresh install). Second service: 'y'.
    result = run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_scripted_input(["maybe", "y"]),
        print_fn=print_fn,
    )
    # Sorted: google_drive (maybe → default no), notion (yes).
    assert "google_drive" in result.disabled_service_ids
    assert "notion" in result.enabled_service_ids


# ---------------------------------------------------------------------------
# State written carries source=setup, updated_by=operator
# ---------------------------------------------------------------------------


def test_interactive_state_provenance_marks_setup_and_operator(tmp_path):
    _captured, print_fn = _capture_print()
    run_first_run(
        data_dir=tmp_path,
        interactive_override=True,
        input_fn=_scripted_input(["y", "y"]),
        print_fn=print_fn,
    )
    store = ServiceStateStore(tmp_path)
    for state in store.list_all():
        assert state.source is ServiceStateSource.SETUP
        assert state.updated_by is ServiceStateUpdatedBy.OPERATOR


def test_non_interactive_state_provenance_marks_setup_and_operator(tmp_path):
    _captured, print_fn = _capture_print()
    run_first_run(
        data_dir=tmp_path,
        all_enabled=True,
        interactive_override=False,
        print_fn=print_fn,
    )
    store = ServiceStateStore(tmp_path)
    for state in store.list_all():
        assert state.source is ServiceStateSource.SETUP
        assert state.updated_by is ServiceStateUpdatedBy.OPERATOR
