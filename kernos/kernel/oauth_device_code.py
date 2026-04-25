"""RFC 8628 device authorization grant client + RFC 7636 PKCE.

Three operations the subsystem exposes:

  - start_device_flow(service): POST to the service's device-
    authorization endpoint with the client_id and scopes; return the
    DeviceCodeStart payload (device_code, user_code, verification_uri,
    expires_in, interval) plus the PKCE verifier when applicable.
  - poll_for_token(service, start, verifier): poll the token endpoint
    at start.interval, honouring authorization_pending / slow_down /
    expired_token / access_denied per RFC 8628 §3.5. Returns the
    issued token bundle (access_token, refresh_token, expires_in,
    scope) when the user completes verification.
  - refresh_credential(member_id, service_id, store): consume the
    stored refresh_token, post grant_type=refresh_token to the token
    endpoint, replace the credential per RFC 6749 §6 (rotate
    refresh_token when the response includes a new one, preserve when
    it does not).

The client uses httpx synchronously. Polling is foreground; the CLI
subcommand (C3) is the consumer. Adapter-side onboarding (Q2 follow-
on) will reuse start_device_flow but provide its own polling
lifecycle.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
    StoredCredential,
)
from kernos.kernel.services import (
    AuthType,
    OAuthDeviceCodeConfig,
    PkceMode,
    ServiceDescriptor,
    ServiceDescriptorError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DeviceCodeError(RuntimeError):
    """Base class for device-code subsystem errors.

    Subclasses correspond to the RFC 8628 / RFC 6749 terminal states
    so callers can branch on the failure mode.
    """


class AuthorizationDeclined(DeviceCodeError):
    """The user denied the authorization request (access_denied)."""


class AuthorizationExpired(DeviceCodeError):
    """The device_code expired before the user completed verification."""


class TokenEndpointError(DeviceCodeError):
    """The token endpoint returned a non-recoverable error.

    Carries the error code returned by the service in the `code`
    attribute so callers can render service-specific guidance.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class DeviceCodeNetworkError(DeviceCodeError):
    """The HTTP transport failed for reasons unrelated to OAuth state."""


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCodeStart:
    """RFC 8628 §3.2 device authorization response.

    `verification_uri_complete` is optional in the RFC and may be
    empty. `pkce_verifier` carries the verifier the same flow needs
    later when polling; empty when PKCE was omitted.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    pkce_verifier: str = ""


@dataclass(frozen=True)
class TokenBundle:
    """RFC 8628 §3.5 / RFC 6749 §5.1 token response."""

    access_token: str
    refresh_token: str
    expires_in: int          # seconds; 0 means no expiry was reported
    scope: str               # space-separated scope string; may be empty
    token_type: str = "Bearer"


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------


def _generate_pkce_verifier_and_challenge() -> tuple[str, str]:
    """Generate a (verifier, S256-challenge) pair per RFC 7636.

    The verifier is 43-128 characters from the unreserved set; we use
    32 bytes of urlsafe-base64 (43 chars after stripping padding).
    The challenge is base64-url(SHA-256(verifier)).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# start_device_flow
# ---------------------------------------------------------------------------


def start_device_flow(
    service: ServiceDescriptor,
    *,
    scopes: list[str] | tuple[str, ...] | None = None,
    http_client: httpx.Client | None = None,
    timeout_seconds: float = 30.0,
) -> DeviceCodeStart:
    """Step 1 of RFC 8628: request a device authorization.

    Posts to service.oauth.device_authorization_uri with the client_id
    (resolved from literal or env var per the descriptor) and scopes.
    Returns the device authorization payload plus the PKCE verifier
    when the service's pkce mode is required or optional.
    """
    if service.auth_type != AuthType.OAUTH_DEVICE_CODE or service.oauth is None:
        raise ServiceDescriptorError(
            f"start_device_flow called on service {service.service_id!r} "
            f"which is not an oauth_device_code service."
        )

    client_id = service.resolve_client_id()
    scope_list = list(scopes) if scopes is not None else list(service.required_scopes)
    payload: dict[str, Any] = {"client_id": client_id}
    if scope_list:
        payload["scope"] = " ".join(scope_list)

    pkce_verifier = ""
    if service.oauth.pkce in (PkceMode.REQUIRED, PkceMode.OPTIONAL):
        pkce_verifier, challenge = _generate_pkce_verifier_and_challenge()
        payload["code_challenge"] = challenge
        payload["code_challenge_method"] = "S256"

    response_payload = _post(
        service.oauth.device_authorization_uri,
        payload,
        http_client=http_client,
        timeout_seconds=timeout_seconds,
    )

    try:
        return DeviceCodeStart(
            device_code=str(response_payload["device_code"]),
            user_code=str(response_payload["user_code"]),
            verification_uri=str(response_payload["verification_uri"]),
            verification_uri_complete=str(
                response_payload.get("verification_uri_complete", "")
            ),
            expires_in=int(response_payload.get("expires_in", 600)),
            interval=int(response_payload.get("interval", 5)),
            pkce_verifier=pkce_verifier,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenEndpointError(
            f"Device authorization response from "
            f"{service.oauth.device_authorization_uri} did not include "
            f"the required fields: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# poll_for_token
# ---------------------------------------------------------------------------


# Default cap to avoid pathological polling under a misbehaving server.
_MAX_POLL_INTERVAL_SECONDS = 60


def poll_for_token(
    service: ServiceDescriptor,
    start: DeviceCodeStart,
    *,
    http_client: httpx.Client | None = None,
    sleeper: Any = None,
    clock: Any = None,
    on_tick: Any = None,
    timeout_seconds: float = 30.0,
) -> TokenBundle:
    """Step 2 of RFC 8628: poll the token endpoint until terminal state.

    Honours the four polling-state error codes per RFC 8628 §3.5:
      - authorization_pending: keep polling at the current interval.
      - slow_down: increase the interval by 5 seconds (RFC default).
      - expired_token: device_code expired; raise AuthorizationExpired.
      - access_denied: user declined; raise AuthorizationDeclined.

    Other token-endpoint errors raise TokenEndpointError carrying the
    error code. Network errors raise DeviceCodeNetworkError.

    `sleeper` and `clock` are seam parameters for tests; they default
    to time.sleep and time.monotonic. `on_tick` is an optional
    callback invoked once per poll with the elapsed seconds, useful
    for the CLI's progress dots.
    """
    if service.auth_type != AuthType.OAUTH_DEVICE_CODE or service.oauth is None:
        raise ServiceDescriptorError(
            f"poll_for_token called on service {service.service_id!r} "
            f"which is not an oauth_device_code service."
        )

    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic

    client_id = service.resolve_client_id()
    interval = max(1, int(start.interval))
    deadline = clock() + max(60, int(start.expires_in))

    # Initial wait so the user has time to enter the code before the
    # first poll.
    sleeper(interval)

    while True:
        if on_tick:
            try:
                on_tick(clock() - (deadline - max(60, int(start.expires_in))))
            except Exception:
                pass

        if clock() > deadline:
            raise AuthorizationExpired(
                "device_code expired before the user completed verification."
            )

        payload: dict[str, Any] = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": start.device_code,
            "client_id": client_id,
        }
        if start.pkce_verifier:
            payload["code_verifier"] = start.pkce_verifier

        try:
            response_payload = _post(
                service.oauth.token_uri,
                payload,
                http_client=http_client,
                timeout_seconds=timeout_seconds,
                tolerate_error_status=True,
            )
        except DeviceCodeNetworkError:
            raise

        if "access_token" in response_payload:
            return TokenBundle(
                access_token=str(response_payload["access_token"]),
                refresh_token=str(response_payload.get("refresh_token", "")),
                expires_in=int(response_payload.get("expires_in", 0) or 0),
                scope=str(response_payload.get("scope", "")),
                token_type=str(response_payload.get("token_type", "Bearer")),
            )

        error = str(response_payload.get("error", "")).strip()
        description = str(response_payload.get("error_description", "")).strip()

        if error == "authorization_pending":
            sleeper(interval)
            continue
        if error == "slow_down":
            interval = min(interval + 5, _MAX_POLL_INTERVAL_SECONDS)
            sleeper(interval)
            continue
        if error == "expired_token":
            raise AuthorizationExpired(
                description or "device_code expired."
            )
        if error == "access_denied":
            raise AuthorizationDeclined(
                description or "the user declined the authorization request."
            )

        raise TokenEndpointError(
            f"Token endpoint returned error {error!r}"
            + (f": {description}" if description else ""),
            code=error,
        )


# ---------------------------------------------------------------------------
# refresh_credential
# ---------------------------------------------------------------------------


def refresh_credential(
    *,
    service: ServiceDescriptor,
    member_id: str,
    store: MemberCredentialStore,
    http_client: httpx.Client | None = None,
    timeout_seconds: float = 30.0,
) -> StoredCredential:
    """Consume the stored refresh_token, mint a new access_token, rotate
    in place per RFC 6749 §6.

    Raises CredentialUnavailable variants:
      - MemberCredentialNotFound when no credential exists for the
        (member, service) pair.
      - TokenEndpointError when the token endpoint rejects the refresh
        (carries the error code; common cases: invalid_grant when the
        user revoked, invalid_client when the client_id changed).
      - DeviceCodeNetworkError when the HTTP call itself fails.

    On success, the stored credential is rotated:
      - access_token replaced.
      - refresh_token replaced when the response includes a new one;
        preserved when it does not (Q6 observe-and-replace).
      - expires_at recomputed from response expires_in.
    """
    if service.auth_type != AuthType.OAUTH_DEVICE_CODE or service.oauth is None:
        raise ServiceDescriptorError(
            f"refresh_credential called on service {service.service_id!r} "
            f"which is not an oauth_device_code service."
        )

    existing = store.get(member_id=member_id, service_id=service.service_id)
    if not existing.refresh_token:
        raise TokenEndpointError(
            f"Stored credential for member={member_id} "
            f"service={service.service_id!r} has no refresh_token. "
            f"Re-run onboarding to obtain a fresh credential.",
            code="no_refresh_token",
        )

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": existing.refresh_token,
        "client_id": service.resolve_client_id(),
    }

    response_payload = _post(
        service.oauth.token_uri,
        payload,
        http_client=http_client,
        timeout_seconds=timeout_seconds,
        tolerate_error_status=True,
    )

    if "access_token" not in response_payload:
        error = str(response_payload.get("error", "")).strip()
        description = str(response_payload.get("error_description", "")).strip()
        raise TokenEndpointError(
            f"Refresh rejected: error={error!r}"
            + (f" description={description}" if description else ""),
            code=error or "refresh_failed",
        )

    new_access = str(response_payload["access_token"])
    new_refresh = str(response_payload.get("refresh_token", "") or existing.refresh_token)
    expires_in = int(response_payload.get("expires_in", 0) or 0)
    expires_at = (int(time.time()) + expires_in) if expires_in > 0 else None

    return store.rotate(
        member_id=member_id,
        service_id=service.service_id,
        token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(
    url: str,
    payload: dict[str, Any],
    *,
    http_client: httpx.Client | None = None,
    timeout_seconds: float = 30.0,
    tolerate_error_status: bool = False,
) -> dict[str, Any]:
    """POST a form-encoded body to `url` and return the parsed JSON.

    OAuth providers expect application/x-www-form-urlencoded for the
    device-authorization and token endpoints. The Accept header asks
    for JSON; nearly every provider returns JSON regardless.

    `tolerate_error_status` is set when polling: 4xx responses on the
    token endpoint carry meaningful error fields per RFC 8628 §3.5
    that the caller wants to inspect rather than convert to an
    exception.
    """
    headers = {"Accept": "application/json"}
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        try:
            response = client.post(url, data=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise DeviceCodeNetworkError(
                f"HTTP request to {url} failed: {exc}"
            ) from exc

        try:
            body = response.json()
        except Exception:
            body = {}

        if response.status_code >= 400 and not tolerate_error_status:
            error = body.get("error", "") if isinstance(body, dict) else ""
            description = body.get("error_description", "") if isinstance(body, dict) else ""
            raise TokenEndpointError(
                f"{url} returned {response.status_code}"
                + (f" error={error!r}" if error else "")
                + (f" description={description}" if description else ""),
                code=str(error or response.status_code),
            )
        if not isinstance(body, dict):
            raise TokenEndpointError(
                f"{url} returned non-object JSON body: {body!r}",
            )
        return body
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass


__all__ = [
    "AuthorizationDeclined",
    "AuthorizationExpired",
    "DeviceCodeError",
    "DeviceCodeNetworkError",
    "DeviceCodeStart",
    "TokenBundle",
    "TokenEndpointError",
    "poll_for_token",
    "refresh_credential",
    "start_device_flow",
]
