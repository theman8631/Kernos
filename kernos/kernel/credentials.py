"""Credential resolution for Anthropic API access.

Supports API keys, OAuth tokens, and optional OpenClaw interop.
"""

import json
import logging
import os

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


def resolve_anthropic_credential() -> str:
    """Resolve an Anthropic API credential from available sources.

    Priority order:
    1. ANTHROPIC_API_KEY env var
    2. ANTHROPIC_OAUTH_TOKEN env var
    3. OpenClaw auth-profiles.json (if OPENCLAW_AUTH_PROFILES_PATH is set)
    4. Empty string (graceful degradation)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.info("Anthropic credential resolved from ANTHROPIC_API_KEY")
        return api_key

    oauth_token = os.getenv("ANTHROPIC_OAUTH_TOKEN", "")
    if oauth_token:
        logger.info("Anthropic credential resolved from ANTHROPIC_OAUTH_TOKEN")
        return oauth_token

    openclaw_path = os.getenv("OPENCLAW_AUTH_PROFILES_PATH", "")
    if openclaw_path:
        credential = _read_openclaw_anthropic_credential(openclaw_path)
        if credential:
            logger.info("Anthropic credential resolved from OpenClaw auth-profiles")
            return credential

    logger.warning("No Anthropic credential found in any source")
    return ""
