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


# ---------------------------------------------------------------------------
# OAuth device-code onboard path
# ---------------------------------------------------------------------------


def _register_slack_service(monkeypatch):
    """Inject a Slack oauth_device_code descriptor into the CLI's stock
    registry by patching the registry loader."""
    from kernos.kernel import credentials_cli as cli_module
    from kernos.kernel.services import ServiceRegistry, parse_service_descriptor

    def _stub_loader():
        registry = ServiceRegistry()
        registry.register(parse_service_descriptor({
            "service_id": "slack",
            "display_name": "Slack",
            "auth_type": "oauth_device_code",
            "operations": ["read_messages", "post_message"],
            "required_scopes": ["chat:write"],
            "oauth": {
                "device_authorization_uri": "https://slack.com/oauth/device",
                "token_uri": "https://slack.com/oauth/token",
                "client_id": "C-test-123",
                "pkce": "optional",
            },
        }))
        return registry

    monkeypatch.setattr(cli_module, "_load_service_registry", _stub_loader)


def test_onboard_device_code_happy_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    _register_slack_service(monkeypatch)

    # Stub the device-code flow.
    from kernos.kernel import oauth_device_code as oauth_module
    from kernos.kernel.oauth_device_code import DeviceCodeStart, TokenBundle

    monkeypatch.setattr(oauth_module, "start_device_flow", lambda service, **kw: DeviceCodeStart(
        device_code="dc-test",
        user_code="ABCD-1234",
        verification_uri="https://slack.com/device",
        verification_uri_complete="https://slack.com/device?code=ABCD-1234",
        expires_in=600, interval=5,
        pkce_verifier="v",
    ))
    monkeypatch.setattr(oauth_module, "poll_for_token", lambda service, start, **kw: TokenBundle(
        access_token="at-from-slack",
        refresh_token="rt-from-slack",
        expires_in=3600,
        scope="chat:write",
    ))

    rc = main(["onboard", "--service", "slack", "--member", "mem_alice"])
    assert rc == 0
    out = capsys.readouterr().out
    # User code + verification URIs surfaced.
    assert "ABCD-1234" in out
    assert "https://slack.com/device" in out
    # Success message at the end.
    assert "OK" in out

    # Credential landed.
    store = MemberCredentialStore(tmp_path, "discord:test")
    cred = store.get(member_id="mem_alice", service_id="slack")
    assert cred.token == "at-from-slack"
    assert cred.refresh_token == "rt-from-slack"
    assert cred.scopes == ("chat:write",)


def test_onboard_device_code_user_declines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    _register_slack_service(monkeypatch)

    from kernos.kernel import oauth_device_code as oauth_module
    from kernos.kernel.oauth_device_code import (
        AuthorizationDeclined, DeviceCodeStart,
    )

    monkeypatch.setattr(oauth_module, "start_device_flow", lambda service, **kw: DeviceCodeStart(
        device_code="dc", user_code="X",
        verification_uri="https://x", verification_uri_complete="",
        expires_in=600, interval=5,
    ))
    def _decline(service, start, **kw):
        raise AuthorizationDeclined("user said no")
    monkeypatch.setattr(oauth_module, "poll_for_token", _decline)

    rc = main(["onboard", "--service", "slack", "--member", "mem_alice"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "declined" in err.lower()


def test_refresh_subcommand_happy_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    _register_slack_service(monkeypatch)

    # Seed a credential to refresh.
    store = MemberCredentialStore(tmp_path, "discord:test")
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old", refresh_token="rt-1",
    )

    from kernos.kernel import credentials_cli as cli_module
    from kernos.kernel.credentials_member import StoredCredential

    def _fake_refresh(*, service, member_id, store, **_kw):
        return StoredCredential(
            service_id="slack", member_id=member_id,
            token="new-access", refresh_token="rt-2",
            expires_at=999999999,
        )
    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential", _fake_refresh,
    )

    rc = main(["refresh", "--service", "slack", "--member", "mem_alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "refreshed" in out.lower()
    assert "expires_at" in out


def test_refresh_subcommand_rejects_api_token_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    # Default registry returns notion (api_token) — refresh should refuse.
    rc = main(["refresh", "--service", "notion", "--member", "mem_alice"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "api_token" in err
    assert "no refresh mechanism" in err.lower()


def test_refresh_subcommand_no_credential(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    _register_slack_service(monkeypatch)

    rc = main(["refresh", "--service", "slack", "--member", "mem_alice"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no credential stored" in err.lower()


def test_onboard_device_code_token_endpoint_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test")
    _register_slack_service(monkeypatch)

    from kernos.kernel import oauth_device_code as oauth_module
    from kernos.kernel.oauth_device_code import (
        DeviceCodeStart, TokenEndpointError,
    )

    monkeypatch.setattr(oauth_module, "start_device_flow", lambda service, **kw: DeviceCodeStart(
        device_code="dc", user_code="X",
        verification_uri="https://x", verification_uri_complete="",
        expires_in=600, interval=5,
    ))
    def _err(service, start, **kw):
        raise TokenEndpointError("invalid_scope: bad scope", code="invalid_scope")
    monkeypatch.setattr(oauth_module, "poll_for_token", _err)

    rc = main(["onboard", "--service", "slack", "--member", "mem_alice"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid_scope" in err
