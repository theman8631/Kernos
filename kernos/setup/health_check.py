"""Startup binary health check.

Asks the runtime chain-builder whether it will actually build. The setup-
time YAML at ``config/llm_chains.yml`` is no longer the source of truth
here — it's wizard bookkeeping for ``kernos setup llm``, not a startup
gate (previous version of this module treated it as one and that gate
drifted from runtime reality; see MODEL-SELECTION-MODULE follow-on).

The check itself is side-effect-free: class imports and pure credential
resolvers only. No provider clients instantiated, no network calls,
no auth pings — so a flaky provider's init path can't break startup
for unrelated providers.

Called from ``kernos start`` and other top-level entry points before
any provider client spins up. Exit 1 on failure. The agent never
starts from a "no chain works" state — the operator has to fix the
env-var chain or credential situation first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from kernos.providers.chains import CanBuildResult, can_build_chains_from_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthCheckResult:
    ok: bool
    reason: str
    primary_spec: str = ""
    resolved: tuple[str, ...] = ()
    unresolved: tuple[tuple[str, str], ...] = ()


def check_llm_chain_health() -> HealthCheckResult:
    """Binary "will the runtime build a working chain?" check.

    Thin wrapper over :func:`can_build_chains_from_env` that packages
    the dry-run result into the module's existing ``HealthCheckResult``
    shape so callers outside this module don't see the surface change.
    """
    dry: CanBuildResult = can_build_chains_from_env()
    return HealthCheckResult(
        ok=dry.ok,
        reason=dry.reason,
        primary_spec=dry.primary_spec,
        resolved=dry.resolved,
        unresolved=dry.unresolved,
    )


def enforce_or_exit() -> None:
    """Health check + secret loading. Call from entry points.

    1. Best-effort: pull secrets from the active storage backend into
       ``os.environ`` so the runtime chain builder finds them.
       Tolerant of missing storage config — users with a plaintext
       ``.env`` skip this layer entirely.
    2. Dry-run ``build_chains_from_env`` for a "will this run?" answer.
       Exit 1 with a structured reason on failure.

    Ordering matters: secrets-first so keychain-stored creds populate
    ``os.environ`` before the dry-run probes env vars.
    """
    # Step 1: best-effort secret hydration (keychain users need this;
    # plaintext-.env users already have secrets in os.environ).
    try:
        from kernos.setup.env_loader import load_secrets_into_env
        load_secrets_into_env()
    except Exception as exc:  # noqa: BLE001 — pre-check, never fatal on its own
        logger.debug("SECRET_HYDRATION_SKIPPED: %s", exc)

    # Step 2: runtime-authoritative dry-run.
    result = check_llm_chain_health()
    if not result.ok:
        import sys

        print("Kernos startup: LLM chain configuration check failed.")
        print(f"  {result.reason}")
        if result.unresolved:
            print("  Details:")
            for spec, reason in result.unresolved:
                print(f"    - {spec}: {reason}")
        sys.exit(1)

    # Log what resolved for operator visibility.
    logger.info(
        "LLM_HEALTH_OK: primary=%s resolved=%s fallback_skipped=%s",
        result.primary_spec, list(result.resolved),
        [spec for spec, _ in result.unresolved],
    )
