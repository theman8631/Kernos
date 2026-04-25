"""RFC 8628 + RFC 6749 §6 + RFC 7636 client tests.

No real network. The httpx client is replaced with a stub that
returns canned responses keyed by URL and the polling sleeper +
clock are seam parameters so tests run instantaneously.
"""

import base64
import hashlib
import time
from typing import Any

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
)
from kernos.kernel.oauth_device_code import (
    AuthorizationDeclined,
    AuthorizationExpired,
    DeviceCodeNetworkError,
    DeviceCodeStart,
    TokenBundle,
    TokenEndpointError,
    _generate_pkce_verifier_and_challenge,
    poll_for_token,
    refresh_credential,
    start_device_flow,
)
from kernos.kernel.services import (
    OAuthDeviceCodeConfig,
    PkceMode,
    parse_service_descriptor,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """Replays scripted responses indexed by (url, sequence_step).

    For polling tests we want different bodies on successive POSTs to
    the same URL; the script is a list of (url_match, response) pairs
    consumed in order. Unmatched calls raise to make missing scripts
    visible.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, headers=None):
        self.calls.append((url, dict(data or {})))
        if not self.script:
            raise AssertionError(
                f"Unexpected POST to {url} with data={data!r}; "
                f"test script exhausted"
            )
        url_match, response = self.script.pop(0)
        if callable(url_match):
            assert url_match(url, dict(data or {})), (
                f"script step did not match POST to {url} data={data!r}"
            )
        else:
            assert url_match in url, (
                f"script step expected url to contain {url_match!r}; "
                f"got {url}"
            )
        return response

    def close(self):
        return None


def _slack_descriptor(pkce: str = "optional", client_id: str = "C-123"):
    return parse_service_descriptor({
        "service_id": "slack",
        "display_name": "Slack",
        "auth_type": "oauth_device_code",
        "operations": ["read_messages", "post_message"],
        "required_scopes": ["chat:write", "channels:read"],
        "oauth": {
            "device_authorization_uri": "https://slack.com/oauth/device",
            "token_uri": "https://slack.com/oauth/token",
            "client_id": client_id,
            "pkce": pkce,
        },
    })


# ---------------------------------------------------------------------------
# PKCE helpers (RFC 7636)
# ---------------------------------------------------------------------------


def test_pkce_helper_generates_43_char_verifier():
    verifier, challenge = _generate_pkce_verifier_and_challenge()
    # Verifier is 32 bytes urlsafe-base64'd minus padding = 43 chars.
    assert len(verifier) == 43
    # Challenge is the same length (SHA-256 → 32 bytes → 43 chars unpadded).
    assert len(challenge) == 43


def test_pkce_challenge_matches_verifier():
    verifier, challenge = _generate_pkce_verifier_and_challenge()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pkce_pairs_are_unique():
    pairs = {_generate_pkce_verifier_and_challenge() for _ in range(50)}
    assert len(pairs) == 50


# ---------------------------------------------------------------------------
# start_device_flow
# ---------------------------------------------------------------------------


def test_start_device_flow_happy_path_with_pkce():
    desc = _slack_descriptor(pkce="optional")
    fake = _FakeClient([
        ("oauth/device", _FakeResponse(200, {
            "device_code": "dc-12345",
            "user_code": "ABCD-1234",
            "verification_uri": "https://slack.com/oauth/device/verify",
            "verification_uri_complete": "https://slack.com/oauth/device/verify?code=ABCD-1234",
            "expires_in": 600,
            "interval": 5,
        })),
    ])
    start = start_device_flow(desc, http_client=fake)
    assert start.device_code == "dc-12345"
    assert start.user_code == "ABCD-1234"
    assert start.verification_uri == "https://slack.com/oauth/device/verify"
    assert start.expires_in == 600
    assert start.interval == 5
    # PKCE verifier was generated.
    assert start.pkce_verifier and len(start.pkce_verifier) == 43
    # Request body included client_id, scope, and PKCE challenge.
    _, payload = fake.calls[0]
    assert payload["client_id"] == "C-123"
    assert "chat:write" in payload["scope"]
    assert payload["code_challenge_method"] == "S256"
    assert "code_challenge" in payload


def test_start_device_flow_omits_pkce_when_mode_omit():
    desc = _slack_descriptor(pkce="omit")
    fake = _FakeClient([
        ("oauth/device", _FakeResponse(200, {
            "device_code": "dc",
            "user_code": "x",
            "verification_uri": "https://x",
            "expires_in": 600, "interval": 5,
        })),
    ])
    start = start_device_flow(desc, http_client=fake)
    assert start.pkce_verifier == ""
    _, payload = fake.calls[0]
    assert "code_challenge" not in payload
    assert "code_challenge_method" not in payload


def test_start_device_flow_required_pkce_includes_challenge():
    desc = _slack_descriptor(pkce="required")
    fake = _FakeClient([
        ("oauth/device", _FakeResponse(200, {
            "device_code": "dc",
            "user_code": "x",
            "verification_uri": "https://x",
            "expires_in": 600, "interval": 5,
        })),
    ])
    start_device_flow(desc, http_client=fake)
    _, payload = fake.calls[0]
    assert "code_challenge" in payload


def test_start_device_flow_raises_on_400_response():
    desc = _slack_descriptor()
    fake = _FakeClient([
        ("oauth/device", _FakeResponse(400, {
            "error": "invalid_client",
            "error_description": "client_id not found",
        })),
    ])
    with pytest.raises(TokenEndpointError, match="invalid_client"):
        start_device_flow(desc, http_client=fake)


def test_start_device_flow_raises_on_missing_required_field():
    desc = _slack_descriptor()
    fake = _FakeClient([
        ("oauth/device", _FakeResponse(200, {
            "user_code": "ABCD-1234",
            # device_code missing
            "verification_uri": "https://x",
        })),
    ])
    with pytest.raises(TokenEndpointError, match="required fields"):
        start_device_flow(desc, http_client=fake)


# ---------------------------------------------------------------------------
# poll_for_token
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic-clock seam that advances on each call by a fixed delta."""

    def __init__(self, delta: float = 1.0):
        self._t = 0.0
        self._delta = delta

    def __call__(self):
        self._t += self._delta
        return self._t


def _build_start(interval=1, expires_in=60, verifier=""):
    return DeviceCodeStart(
        device_code="dc-xyz",
        user_code="ABCD-1234",
        verification_uri="https://example.com/device",
        verification_uri_complete="",
        expires_in=expires_in,
        interval=interval,
        pkce_verifier=verifier,
    )


def test_poll_for_token_happy_path():
    desc = _slack_descriptor()
    start = _build_start()
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(200, {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "chat:write",
            "token_type": "Bearer",
        })),
    ])
    sleeps: list[float] = []
    bundle = poll_for_token(
        desc, start,
        http_client=fake,
        sleeper=lambda s: sleeps.append(s),
        clock=lambda: 0.0,
    )
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert bundle.expires_in == 3600
    # Initial wait of `interval` seconds before the first poll.
    assert sleeps == [1]


def test_poll_for_token_authorization_pending_then_success():
    desc = _slack_descriptor()
    start = _build_start(interval=2)
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {"error": "authorization_pending"})),
        ("oauth/token", _FakeResponse(400, {"error": "authorization_pending"})),
        ("oauth/token", _FakeResponse(200, {
            "access_token": "at-final", "expires_in": 3600,
        })),
    ])
    sleeps: list[float] = []
    bundle = poll_for_token(
        desc, start,
        http_client=fake,
        sleeper=lambda s: sleeps.append(s),
        clock=lambda: 0.0,
    )
    assert bundle.access_token == "at-final"
    # Initial sleep + sleep between each pending response.
    assert sleeps == [2, 2, 2]


def test_poll_for_token_slow_down_increases_interval():
    desc = _slack_descriptor()
    start = _build_start(interval=2)
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {"error": "slow_down"})),
        ("oauth/token", _FakeResponse(200, {
            "access_token": "at-final", "expires_in": 3600,
        })),
    ])
    sleeps: list[float] = []
    poll_for_token(
        desc, start,
        http_client=fake,
        sleeper=lambda s: sleeps.append(s),
        clock=lambda: 0.0,
    )
    # Initial sleep (2), then slow_down adds 5 → 7.
    assert sleeps == [2, 7]


def test_poll_for_token_expired_token_raises():
    desc = _slack_descriptor()
    start = _build_start()
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {"error": "expired_token"})),
    ])
    with pytest.raises(AuthorizationExpired):
        poll_for_token(
            desc, start,
            http_client=fake,
            sleeper=lambda s: None,
            clock=lambda: 0.0,
        )


def test_poll_for_token_access_denied_raises():
    desc = _slack_descriptor()
    start = _build_start()
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {"error": "access_denied"})),
    ])
    with pytest.raises(AuthorizationDeclined):
        poll_for_token(
            desc, start,
            http_client=fake,
            sleeper=lambda s: None,
            clock=lambda: 0.0,
        )


def test_poll_for_token_includes_pkce_verifier_in_payload():
    desc = _slack_descriptor(pkce="required")
    start = _build_start(verifier="my-verifier")
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(200, {
            "access_token": "at", "expires_in": 3600,
        })),
    ])
    poll_for_token(
        desc, start,
        http_client=fake,
        sleeper=lambda s: None,
        clock=lambda: 0.0,
    )
    _, payload = fake.calls[0]
    assert payload["code_verifier"] == "my-verifier"


def test_poll_for_token_omits_verifier_when_none():
    desc = _slack_descriptor(pkce="omit")
    start = _build_start(verifier="")
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(200, {
            "access_token": "at", "expires_in": 3600,
        })),
    ])
    poll_for_token(
        desc, start,
        http_client=fake,
        sleeper=lambda s: None,
        clock=lambda: 0.0,
    )
    _, payload = fake.calls[0]
    assert "code_verifier" not in payload


def test_poll_for_token_unknown_error_raises_token_endpoint_error():
    desc = _slack_descriptor()
    start = _build_start()
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {
            "error": "weird_provider_specific_error",
            "error_description": "the moon was full",
        })),
    ])
    with pytest.raises(TokenEndpointError, match="weird_provider_specific_error"):
        poll_for_token(
            desc, start,
            http_client=fake,
            sleeper=lambda s: None,
            clock=lambda: 0.0,
        )


def test_poll_for_token_deadline_raises_authorization_expired():
    desc = _slack_descriptor()
    start = _build_start(interval=1, expires_in=2)
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {"error": "authorization_pending"})),
        ("oauth/token", _FakeResponse(400, {"error": "authorization_pending"})),
        ("oauth/token", _FakeResponse(400, {"error": "authorization_pending"})),
    ])
    # Clock advances 100s on every call so we hit the deadline almost
    # immediately. The expires_in floor is 60s in poll_for_token, so
    # any clock returning >60 after the start triggers the deadline.
    deadline_clock = iter([0, 0, 100, 200, 300])
    with pytest.raises(AuthorizationExpired):
        poll_for_token(
            desc, start,
            http_client=fake,
            sleeper=lambda s: None,
            clock=lambda: next(deadline_clock),
        )


# ---------------------------------------------------------------------------
# refresh_credential
# ---------------------------------------------------------------------------


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def store(tmp_path, env_key):
    return MemberCredentialStore(tmp_path, "discord:i")


def test_refresh_credential_replaces_access_token(store):
    desc = _slack_descriptor()
    store.add(
        member_id="mem_alice",
        service_id="slack",
        token="old-access",
        refresh_token="rt-1",
        expires_at=int(time.time()) - 60,
    )
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(200, {
            "access_token": "new-access",
            "expires_in": 3600,
        })),
    ])
    rotated = refresh_credential(
        service=desc, member_id="mem_alice", store=store, http_client=fake,
    )
    assert rotated.token == "new-access"
    # Refresh token preserved when not rotated by the server (Q6).
    assert rotated.refresh_token == "rt-1"
    assert rotated.expires_at and rotated.expires_at > int(time.time())


def test_refresh_credential_rotates_refresh_token_when_returned(store):
    desc = _slack_descriptor()
    store.add(
        member_id="mem_alice",
        service_id="slack",
        token="old-access",
        refresh_token="rt-1",
    )
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(200, {
            "access_token": "new-access",
            "refresh_token": "rt-2",
            "expires_in": 3600,
        })),
    ])
    rotated = refresh_credential(
        service=desc, member_id="mem_alice", store=store, http_client=fake,
    )
    assert rotated.refresh_token == "rt-2"


def test_refresh_credential_raises_when_server_rejects(store):
    desc = _slack_descriptor()
    store.add(
        member_id="mem_alice",
        service_id="slack",
        token="old-access",
        refresh_token="rt-1",
    )
    fake = _FakeClient([
        ("oauth/token", _FakeResponse(400, {
            "error": "invalid_grant",
            "error_description": "user revoked the token",
        })),
    ])
    with pytest.raises(TokenEndpointError, match="invalid_grant"):
        refresh_credential(
            service=desc, member_id="mem_alice", store=store, http_client=fake,
        )
    # Stored credential preserved on failure (caller decides to revoke).
    cred = store.get(member_id="mem_alice", service_id="slack")
    assert cred.token == "old-access"


def test_refresh_credential_raises_when_no_refresh_token_stored(store):
    desc = _slack_descriptor()
    store.add(
        member_id="mem_alice",
        service_id="slack",
        token="t",
        refresh_token="",
    )
    fake = _FakeClient([])
    with pytest.raises(TokenEndpointError, match="no refresh_token"):
        refresh_credential(
            service=desc, member_id="mem_alice", store=store, http_client=fake,
        )


def test_refresh_credential_raises_when_no_credential_stored(store):
    desc = _slack_descriptor()
    fake = _FakeClient([])
    with pytest.raises(MemberCredentialNotFound):
        refresh_credential(
            service=desc, member_id="mem_alice", store=store, http_client=fake,
        )
