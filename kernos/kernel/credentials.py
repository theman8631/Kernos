"""Credential resolution for Anthropic and OpenAI Codex API access.

Supports API keys, OAuth tokens, Claude CLI credentials, OpenClaw interop,
and ChatGPT Codex OAuth credentials.
"""

import base64
import json
import logging
import os
import time
from typing import TypedDict

from kernos.kernel.exceptions import ReasoningConnectionError, ReasoningProviderError

logger = logging.getLogger(__name__)


def _read_openclaw_anthropic_credential(path: str) -> str | None:
    """Read an Anthropic credential from an OpenClaw auth-profiles.json file.

    Returns the token/key string, or None on any failure.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("OpenClaw auth-profiles not found: %s", path)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("OpenClaw auth-profiles unreadable: %s (%s)", path, exc)
        return None

    try:
        last_good = data["lastGood"]
        profile_name = last_good.get("anthropic")
        if not profile_name:
            logger.warning("OpenClaw auth-profiles has no anthropic in lastGood")
            return None

        profile = data["profiles"][profile_name]
        profile_type = profile.get("type", "")
        if profile_type == "token":
            credential = profile.get("token")
        elif profile_type == "api_key":
            credential = profile.get("key")
        else:
            logger.warning("OpenClaw profile %s has unknown type: %s", profile_name, profile_type)
            return None

        if credential:
            return credential
        logger.warning("OpenClaw profile %s has empty credential", profile_name)
        return None
    except (KeyError, TypeError) as exc:
        logger.warning("OpenClaw auth-profiles malformed: %s", exc)
        return None


def _read_claude_cli_credential() -> str | None:
    """Exchange Claude CLI OAuth token for a short-lived API key.

    Claude Code stores OAuth tokens in ~/.claude/.credentials.json tied to a
    Claude Max subscription. The Anthropic API doesn't accept OAuth tokens
    directly — Claude Code exchanges them for API keys via a dedicated endpoint.
    We do the same: POST the OAuth token to /api/oauth/claude_cli/create_api_key
    and get back a usable sk-ant-* API key.
    """
    creds_path = os.path.expanduser("~/.claude/.credentials.json")
    try:
        with open(creds_path) as f:
            creds = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    oauth_data = creds.get("claudeAiOauth", {})
    access_token = oauth_data.get("accessToken", "")
    if not access_token:
        return None

    # Check expiry (expiresAt is milliseconds since epoch)
    expires_at = oauth_data.get("expiresAt", 0)
    if expires_at and expires_at <= time.time() * 1000:
        logger.warning(
            "Claude CLI OAuth token expired at %s",
            time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at / 1000)),
        )
        return None

    # Exchange OAuth token for a short-lived API key
    try:
        import urllib.request
        import urllib.error

        url = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        api_key = result.get("api_key", "") or result.get("key", "")
        if api_key:
            logger.info(
                "Anthropic credential resolved from Claude CLI OAuth → API key "
                "(subscription: %s, tier: %s)",
                oauth_data.get("subscriptionType", "?"),
                oauth_data.get("rateLimitTier", "?"),
            )
            return api_key

        logger.warning("Claude CLI OAuth key exchange returned no api_key: %s", result)
        return None

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Claude CLI OAuth key exchange failed: %s", exc)
        return None


def resolve_anthropic_credential() -> str:
    """Resolve an Anthropic API credential from available sources.

    Priority order:
    1. ANTHROPIC_API_KEY env var
    2. ANTHROPIC_OAUTH_TOKEN env var
    3. Claude CLI credentials (~/.claude/.credentials.json)
    4. OpenClaw auth-profiles.json (if OPENCLAW_AUTH_PROFILES_PATH is set)
    5. Empty string (graceful degradation)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.info("Anthropic credential resolved from ANTHROPIC_API_KEY")
        return api_key

    oauth_token = os.getenv("ANTHROPIC_OAUTH_TOKEN", "")
    if oauth_token:
        logger.info("Anthropic credential resolved from ANTHROPIC_OAUTH_TOKEN")
        return oauth_token

    # Claude CLI OAuth — Max subscription, no credit balance needed
    cli_token = _read_claude_cli_credential()
    if cli_token:
        return cli_token

    openclaw_path = os.getenv("OPENCLAW_AUTH_PROFILES_PATH", "")
    if openclaw_path:
        credential = _read_openclaw_anthropic_credential(openclaw_path)
        if credential:
            logger.info("Anthropic credential resolved from OpenClaw auth-profiles")
            return credential

    logger.warning("No Anthropic credential found in any source")
    return ""


# ---------------------------------------------------------------------------
# OpenAI Codex OAuth credentials
# ---------------------------------------------------------------------------


class OpenAICodexCredential(TypedDict):
    """ChatGPT Codex OAuth credential shape."""
    access: str
    refresh: str
    expires: int       # Milliseconds since epoch
    accountId: str


_CODEX_CREDS_PATH = ".credentials/openai-codex.json"
_CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _decode_jwt_account_id(token: str) -> str:
    """Extract chatgpt_account_id from a ChatGPT OAuth JWT access token.

    Reads the claim at https://api.openai.com/auth -> chatgpt_account_id.
    Does NOT verify the signature — only decodes the payload for field extraction.
    """
    try:
        # JWT: header.payload.signature — decode the payload (part 1)
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("Not a valid JWT")
        payload_b64 = parts[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        auth_claim = payload.get("https://api.openai.com/auth", {})
        account_id = auth_claim.get("chatgpt_account_id", "")
        if not account_id:
            raise ValueError("chatgpt_account_id not found in JWT")
        return account_id
    except Exception as exc:
        logger.warning("Failed to extract accountId from Codex JWT: %s", exc)
        raise ValueError(f"Cannot extract accountId from JWT: {exc}") from exc


def resolve_openai_codex_credential() -> OpenAICodexCredential:
    """Resolve OpenAI Codex OAuth credentials.

    Priority:
    1. Environment variables (OPENAI_CODEX_ACCESS_TOKEN, etc.)
    2. Local credential file (.credentials/openai-codex.json)
    """
    # Priority 1: Environment variables
    access = os.getenv("OPENAI_CODEX_ACCESS_TOKEN", "")
    refresh = os.getenv("OPENAI_CODEX_REFRESH_TOKEN", "")
    expires_str = os.getenv("OPENAI_CODEX_EXPIRES", "")
    account_id = os.getenv("OPENAI_CODEX_ACCOUNT_ID", "")

    if access and refresh:
        expires = int(expires_str) if expires_str else 0
        if not account_id:
            account_id = _decode_jwt_account_id(access)
        logger.info("OpenAI Codex credential resolved from environment")
        return OpenAICodexCredential(
            access=access, refresh=refresh, expires=expires, accountId=account_id,
        )

    # Priority 2: Local credential file
    creds_path = os.getenv("OPENAI_CODEX_CREDS_PATH", _CODEX_CREDS_PATH)
    try:
        with open(creds_path) as f:
            data = json.load(f)
        access = data["access"]
        refresh = data["refresh"]
        expires = data.get("expires", 0)
        account_id = data.get("accountId", "")
        if not account_id:
            account_id = _decode_jwt_account_id(access)
        logger.info("OpenAI Codex credential resolved from %s", creds_path)
        return OpenAICodexCredential(
            access=access, refresh=refresh, expires=expires, accountId=account_id,
        )
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("OpenAI Codex credential file malformed: %s", exc)

    raise ValueError(
        "No OpenAI Codex credentials found. Set OPENAI_CODEX_ACCESS_TOKEN + "
        "OPENAI_CODEX_REFRESH_TOKEN env vars, or create .credentials/openai-codex.json"
    )


async def refresh_openai_codex_credential(
    creds: OpenAICodexCredential,
) -> OpenAICodexCredential:
    """Refresh an expired Codex OAuth access token.

    POSTs to OpenAI's OAuth token endpoint with the refresh token.
    Returns updated credentials with new access token, expiry, and accountId.
    """
    import urllib.request
    import urllib.error
    import urllib.parse

    form_data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh"],
        "client_id": _CODEX_OAUTH_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        _CODEX_OAUTH_TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form_data,
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise ReasoningConnectionError(f"Codex token refresh failed: {exc}") from exc

    new_access = result.get("access_token", "")
    new_refresh = result.get("refresh_token", creds["refresh"])
    expires_in = result.get("expires_in", 3600)  # seconds
    new_expires = int((time.time() + expires_in) * 1000)

    if not new_access:
        raise ReasoningProviderError("Codex token refresh returned no access_token")

    new_account_id = _decode_jwt_account_id(new_access)

    new_creds = OpenAICodexCredential(
        access=new_access,
        refresh=new_refresh,
        expires=new_expires,
        accountId=new_account_id,
    )

    # Persist if file-backed
    creds_path = os.getenv("OPENAI_CODEX_CREDS_PATH", _CODEX_CREDS_PATH)
    try:
        os.makedirs(os.path.dirname(creds_path) or ".", exist_ok=True)
        with open(creds_path, "w") as f:
            json.dump(dict(new_creds), f, indent=2)
        logger.info("Codex credentials refreshed and persisted to %s", creds_path)
    except OSError as exc:
        logger.warning("Could not persist refreshed Codex credentials: %s", exc)

    return new_creds
