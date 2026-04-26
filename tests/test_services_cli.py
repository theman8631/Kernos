"""Tests for the top-level `kernos services` subcommand.

Covers Section 3 of the INSTALL-FOR-STOCK-CONNECTORS spec.

The CLI surfaces in `kernos --help` (verified separately by
existence checks); these tests exercise the subcommand handlers
directly with synthetic args to avoid spawning subprocesses.
"""

from __future__ import annotations

import argparse

import pytest

from kernos.kernel.services import ServiceRegistry
from kernos.setup.service_state import (
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
)
from kernos.setup.services_cli import (
    cmd_services_disable,
    cmd_services_enable,
    cmd_services_info,
    cmd_services_list,
    dispatch_services,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_services_list_shows_all_registered(capsys, data_dir):
    rc = cmd_services_list(_args(data_dir=str(data_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "notion" in out
    assert "google_drive" in out
    # Fresh install — everything unset.
    assert "[unset" in out


def test_services_list_distinguishes_enabled_disabled_unset(capsys, data_dir):
    store = ServiceStateStore(data_dir)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    store.set(
        "google_drive",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )

    cmd_services_list(_args(data_dir=str(data_dir)))
    out = capsys.readouterr().out

    # Pull individual lines for each service.
    notion_line = next(l for l in out.splitlines() if "notion" in l and "google_drive" not in l)
    drive_line = next(l for l in out.splitlines() if "google_drive" in l)
    assert "[enabled" in notion_line
    assert "[disabled" in drive_line


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


def test_services_enable_writes_state_with_operator_provenance(
    capsys, data_dir
):
    rc = cmd_services_enable(
        _args(
            service_id="notion",
            data_dir=str(data_dir),
            reason="onboarding member",
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "notion enabled" in out

    store = ServiceStateStore(data_dir)
    state = store.get("notion")
    assert state is not None
    assert state.enabled is True
    assert state.source is ServiceStateSource.OPERATOR
    assert state.updated_by is ServiceStateUpdatedBy.OPERATOR
    assert state.reason == "onboarding member"


def test_services_disable_persists_and_hints_at_credential_revoke(
    capsys, data_dir
):
    rc = cmd_services_disable(
        _args(
            service_id="notion",
            data_dir=str(data_dir),
            reason="",
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "notion disabled" in out
    assert "kernos credentials revoke" in out
    assert ServiceStateStore(data_dir).is_enabled("notion") is False


def test_services_enable_rejects_unknown_service(capsys, data_dir):
    rc = cmd_services_enable(
        _args(
            service_id="nonexistent",
            data_dir=str(data_dir),
            reason="",
        )
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "not registered" in out
    # Operator gets a list of valid services to correct the typo.
    assert "Available services" in out


def test_services_enable_rejects_empty_service_id(capsys, data_dir):
    rc = cmd_services_enable(
        _args(service_id="   ", data_dir=str(data_dir), reason="")
    )
    assert rc == 2


def test_services_disable_rejects_unknown_service(capsys, data_dir):
    rc = cmd_services_disable(
        _args(service_id="nonexistent", data_dir=str(data_dir), reason="")
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_services_info_shows_descriptor_and_install_state(capsys, data_dir):
    store = ServiceStateStore(data_dir)
    store.set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
        reason="picked at first-run",
    )

    rc = cmd_services_info(
        _args(service_id="notion", data_dir=str(data_dir))
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "notion" in out
    assert "Auth type" in out
    assert "api_token" in out
    assert "Install state: enabled" in out
    assert "source     : setup" in out
    assert "picked at first-run" in out
    assert "Onboarding next step" in out
    assert "kernos credentials onboard --service notion" in out


def test_services_info_shows_unset_state_for_fresh_install(capsys, data_dir):
    rc = cmd_services_info(
        _args(service_id="notion", data_dir=str(data_dir))
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Install state: unset" in out


def test_services_info_rejects_unknown_service(capsys, data_dir):
    rc = cmd_services_info(
        _args(service_id="ghost", data_dir=str(data_dir))
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "not registered" in out


# ---------------------------------------------------------------------------
# dispatch_services
# ---------------------------------------------------------------------------


def test_dispatch_services_routes_to_list(capsys, data_dir):
    rc = dispatch_services(
        _args(services_command="list", data_dir=str(data_dir))
    )
    assert rc == 0
    assert "notion" in capsys.readouterr().out


def test_dispatch_services_routes_to_enable(capsys, data_dir):
    rc = dispatch_services(
        _args(
            services_command="enable",
            service_id="notion",
            data_dir=str(data_dir),
            reason="",
        )
    )
    assert rc == 0
    assert ServiceStateStore(data_dir).is_enabled("notion") is True


def test_dispatch_services_prints_usage_for_missing_subcommand(capsys, data_dir):
    rc = dispatch_services(_args(services_command=""))
    assert rc == 1
    out = capsys.readouterr().out
    assert "kernos services list" in out
    assert "kernos services enable" in out
    assert "kernos services disable" in out
    assert "kernos services info" in out


# ---------------------------------------------------------------------------
# Top-level CLI surfaces the new commands
# ---------------------------------------------------------------------------


def test_top_level_kernos_help_includes_services_and_credentials():
    """Acceptance criterion 5 + 6: kernos --help surfaces services
    and credentials. Validate via direct argparse inspection rather
    than spawning a subprocess."""
    import io
    from contextlib import redirect_stdout
    from kernos import cli

    buf = io.StringIO()
    with pytest.raises(SystemExit):
        with redirect_stdout(buf):
            cli.main.__wrapped__ if hasattr(cli.main, "__wrapped__") else None
            # argparse exits on --help; we capture and inspect.
            import sys as _sys
            saved = _sys.argv
            _sys.argv = ["kernos", "--help"]
            try:
                cli.main()
            finally:
                _sys.argv = saved
    out = buf.getvalue()
    assert "services" in out
    assert "credentials" in out
