"""Tests for the credential onboarding CLI."""

import io
import sys

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_cli import main
from kernos.kernel.credentials_member import MemberCredentialStore


@pytest.fixture(autouse=True)
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


def test_info_subcommand_runs_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    rc = main(["info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "data dir:" in out
    assert "stock services available" in out
    assert "notion" in out


def test_onboard_rejects_unknown_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    rc = main(["onboard", "--service", "ghost", "--member", "mem"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ghost" in err
    assert "not registered" in err


def test_onboard_api_token_via_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")

    # Replace getpass.getpass with a stub that returns a known token.
    monkeypatch.setattr(
        "kernos.kernel.credentials_cli.getpass.getpass",
        lambda prompt: "secret_xyz",
    )
    rc = main([
        "onboard", "--service", "notion", "--member", "mem_alice",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    # Credential is stored.
    store = MemberCredentialStore(tmp_path, "discord:test")
    cred = store.get(member_id="mem_alice", service_id="notion")
    assert cred.token == "secret_xyz"


def test_onboard_refuses_when_no_token_provided(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    monkeypatch.setattr(
        "kernos.kernel.credentials_cli.getpass.getpass",
        lambda prompt: "",
    )
    rc = main(["onboard", "--service", "notion", "--member", "m"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no token" in err.lower()


def test_revoke_returns_ok_when_credential_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")

    store = MemberCredentialStore(tmp_path, "discord:test")
    store.add(member_id="mem_alice", service_id="notion", token="x")

    rc = main(["revoke", "--service", "notion", "--member", "mem_alice"])
    assert rc == 0
    assert "revoked" in capsys.readouterr().out

    # Credential gone.
    assert not store.has(member_id="mem_alice", service_id="notion")


def test_list_shows_member_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")

    store = MemberCredentialStore(tmp_path, "discord:test")
    store.add(member_id="mem_alice", service_id="notion", token="x")

    rc = main(["list", "--member", "mem_alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notion" in out


def test_missing_instance_id_errors_with_clear_message(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    # The instance resolver calls sys.exit(2) on missing identifier.
    with pytest.raises(SystemExit) as excinfo:
        main(["info"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "instance" in err.lower()


def test_no_command_prints_help(capsys):
    rc = main([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "onboard" in out
    assert "revoke" in out
