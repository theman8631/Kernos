"""Aider builder — hand workspace build tasks to the Aider CLI.

Aider ships as a Python dependency (``aider-chat``), picks up Kernos's
existing LLM credentials where the provider is compatible, and runs
inside the same scope wrapper as the native backend when
``KERNOS_WORKSPACE_SCOPE=isolated``.

The adapter is pragmatic rather than deep:

* **Install-as-dependency** — aider is ``pip install``-ed alongside Kernos.
  No binary on PATH at a known location; we resolve ``aider`` via
  :func:`shutil.which` at call time.
* **Credential pass-through** — the adapter translates Kernos's
  ``KERNOS_LLM_PROVIDER`` + provider key into the env vars Aider reads
  natively. ``AIDER_MODEL`` and ``AIDER_API_KEY`` let the operator override
  for providers Aider doesn't natively share with Kernos (Codex OAuth,
  Ollama).
* **Scope via sitecustomize** — external Python subprocesses don't accept
  a prepended preamble, so we copy ``sitecustomize.py`` into
  ``{space_dir}/.kernos_sandbox/`` and prepend that directory to
  ``PYTHONPATH`` in the subprocess env. Python's own startup sequence
  imports sitecustomize before Aider's entry point runs.
* **File modification tracking** — mtime snapshot of ``space_dir`` before
  and after invocation; the diff populates ``BuildResult.files_modified``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kernos.kernel.builders.base import BuildResult
from kernos.utils import _safe_name

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300
DEFAULT_TIMEOUT = 60
STDOUT_BUDGET = 4000
STDERR_BUDGET = 2000

_PREAMBLE_SUBDIR = ".kernos_sandbox"
_PREAMBLE_FILENAME = "sandbox_preamble.py"
_SITECUSTOMIZE_FILENAME = "sitecustomize.py"

#: Mapping from model-name prefix → Aider-native env var. Used when the
#: operator supplies ``AIDER_API_KEY`` and ``AIDER_MODEL`` but no explicit
#: provider hint — we infer from the model string what kind of key it is.
_MODEL_PREFIX_TO_ENV: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sonnet", "haiku", "opus", "claude"), "ANTHROPIC_API_KEY"),
    (("gpt-", "o1-", "o3-", "4o"), "OPENAI_API_KEY"),
    (("gemini",), "GEMINI_API_KEY"),
    (("deepseek",), "DEEPSEEK_API_KEY"),
    (("groq",), "GROQ_API_KEY"),
)


def _api_key_env_for_model(model: str) -> str:
    """Infer the Aider-native env var name from a model string.

    Returns an empty string if the model prefix isn't recognized; callers
    then decide whether to fall through to a provider-default or report a
    config error.
    """
    m = model.lower()
    if m.startswith("ollama_chat/") or m.startswith("ollama/"):
        return ""  # Ollama: no API key needed
    for prefixes, env_var in _MODEL_PREFIX_TO_ENV:
        for p in prefixes:
            if m.startswith(p):
                return env_var
    return ""


#: Anthropic primary model is a class constant on the provider, not an env
#: var. Reading the provider module directly avoids circular imports from
#: ``kernos.providers.chains.build_chains_from_env``. If this constant
#: drifts from the Kernos primary, the default here drifts with it.
def _kernos_primary_model_for_aider() -> str:
    """Return the aider-compatible model string for Kernos's primary chain.

    Mirrors the model the Kernos reasoning service would pick as its
    primary, translated into the shape aider / litellm expects:

      * anthropic → ``AnthropicProvider.main_model`` (e.g. ``claude-sonnet-4-6``)
      * ollama    → ``ollama_chat/<OLLAMA_MODEL>`` (litellm prefix)
      * codex     → "" (aider cannot consume Codex OAuth tokens)

    Returning "" means "can't mirror Kernos's primary; caller should fall
    through to the error path or honor an operator override."

    **Future setup hook.** The operator can override this at any time by
    setting ``AIDER_MODEL`` in their ``.env``. A future ``kernos setup llm``
    extension could add a dedicated Aider-model prompt that writes
    ``AIDER_MODEL=...`` — no code change here required.
    """
    provider = (os.getenv("KERNOS_LLM_PROVIDER", "") or "anthropic").strip().lower()
    if provider == "anthropic":
        # Pull the class attribute from the provider module directly to stay
        # in sync with Kernos's chain defaults without instantiating the
        # provider (which would require a real API key).
        try:
            from kernos.providers.anthropic_provider import AnthropicProvider

            return getattr(AnthropicProvider, "main_model", "") or ""
        except Exception:
            return ""
    if provider == "ollama":
        m = (os.getenv("OLLAMA_MODEL", "") or "").strip()
        if not m:
            return ""
        if m.startswith(("ollama_chat/", "ollama/")):
            return m
        return f"ollama_chat/{m}"
    # Codex and any other provider: not directly consumable by aider.
    return ""


def _resolve_aider_config() -> dict[str, Any]:
    """Decide which model + which env vars Aider will run with.

    Returns a dict:
        {"model": str, "env_updates": {env_var: value}, "error": str | None}

    * ``error`` is non-empty and ``model`` is ``""`` when the adapter
      cannot proceed (missing credentials, unsupported provider without
      an override).
    * ``error`` is empty and ``model`` is set when the adapter should
      proceed using ``model`` with ``env_updates`` merged into the
      subprocess env.
    """
    aider_model = (os.getenv("AIDER_MODEL", "") or "").strip()
    aider_api_key = (os.getenv("AIDER_API_KEY", "") or "").strip()
    provider = (os.getenv("KERNOS_LLM_PROVIDER", "") or "anthropic").strip().lower()

    # Case 1: explicit AIDER_MODEL override
    if aider_model:
        # Ollama: both local and cloud. Local needs no creds; cloud needs
        # OLLAMA_API_BASE + OLLAMA_API_KEY. Pass through whatever the
        # operator has set — aider / litellm reads these env vars natively.
        if aider_model.lower().startswith(("ollama_chat/", "ollama/")):
            ollama_env: dict[str, str] = {}
            # litellm reads OLLAMA_API_BASE; Kernos's .env often uses
            # OLLAMA_BASE_URL. Translate if only the latter is set.
            api_base = (os.getenv("OLLAMA_API_BASE", "") or "").strip()
            if not api_base:
                api_base = (os.getenv("OLLAMA_BASE_URL", "") or "").strip()
            if api_base:
                ollama_env["OLLAMA_API_BASE"] = api_base
            api_key = (os.getenv("OLLAMA_API_KEY", "") or "").strip()
            if api_key:
                ollama_env["OLLAMA_API_KEY"] = api_key
            return {
                "model": aider_model,
                "env_updates": ollama_env,
                "error": None,
            }

        if aider_api_key:
            env_var = _api_key_env_for_model(aider_model)
            if not env_var:
                # Unknown prefix — set the generic pass-through AIDER_API_KEY
                # and hope the operator knows what they're doing.
                return {
                    "model": aider_model,
                    "env_updates": {"AIDER_API_KEY": aider_api_key},
                    "error": None,
                }
            return {
                "model": aider_model,
                "env_updates": {env_var: aider_api_key},
                "error": None,
            }

        # AIDER_MODEL but no AIDER_API_KEY — try to match against an existing
        # provider credential that Kernos already has (e.g. operator wants
        # sonnet-3-5 instead of sonnet default, uses same Anthropic key).
        inferred_env = _api_key_env_for_model(aider_model)
        if inferred_env:
            existing = os.getenv(inferred_env, "").strip()
            if existing:
                return {
                    "model": aider_model,
                    "env_updates": {inferred_env: existing},
                    "error": None,
                }
        return {
            "model": "",
            "env_updates": {},
            "error": (
                f"AIDER_MODEL={aider_model!r} is set but no API key is available "
                f"(AIDER_API_KEY is unset and no matching provider env var was found). "
                f"See /docs/install.md for Aider credential setup."
            ),
        }

    # Case 2: no override — mirror Kernos's primary chain when possible
    mirrored = _kernos_primary_model_for_aider()

    if provider == "anthropic":
        anthropic_key = (os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
        if not anthropic_key:
            return {
                "model": "",
                "env_updates": {},
                "error": (
                    "Aider backend requires ANTHROPIC_API_KEY when KERNOS_LLM_PROVIDER=anthropic "
                    "and AIDER_MODEL is unset. See /docs/install.md."
                ),
            }
        # Use Kernos's primary Anthropic model (e.g. claude-sonnet-4-6) so
        # aider reasons with the same model Kernos's principal agent uses.
        # Falls back to aider's ``sonnet`` alias if we somehow can't resolve
        # the primary.
        return {
            "model": mirrored or "sonnet",
            "env_updates": {"ANTHROPIC_API_KEY": anthropic_key},
            "error": None,
        }

    if provider == "ollama":
        # Mirror Kernos's primary ollama model when OLLAMA_MODEL is set.
        # Without it, there's no sensible default — error explicitly so the
        # operator sees why.
        if not mirrored:
            return {
                "model": "",
                "env_updates": {},
                "error": (
                    "Aider backend requires AIDER_MODEL or OLLAMA_MODEL when "
                    "KERNOS_LLM_PROVIDER=ollama. See /docs/install.md."
                ),
            }
        ollama_env: dict[str, str] = {}
        api_base = (os.getenv("OLLAMA_API_BASE", "") or "").strip()
        if not api_base:
            api_base = (os.getenv("OLLAMA_BASE_URL", "") or "").strip()
        if api_base:
            ollama_env["OLLAMA_API_BASE"] = api_base
        api_key = (os.getenv("OLLAMA_API_KEY", "") or "").strip()
        if api_key:
            ollama_env["OLLAMA_API_KEY"] = api_key
        return {
            "model": mirrored,
            "env_updates": ollama_env,
            "error": None,
        }

    # Case 3: codex / anything else without override — explicit error.
    # Codex OAuth tokens are not standard OpenAI API keys; aider / litellm
    # cannot consume them directly. The operator must set AIDER_MODEL +
    # AIDER_API_KEY (or move Kernos to an aider-compatible provider).
    return {
        "model": "",
        "env_updates": {},
        "error": (
            f"Aider backend requires AIDER_MODEL and AIDER_API_KEY when "
            f"KERNOS_LLM_PROVIDER={provider!r} (Aider does not natively share "
            f"credentials with this provider). See /docs/install.md."
        ),
    }


def _install_sandbox_artifacts(space_dir: str) -> str:
    """Copy preamble + sitecustomize into ``.kernos_sandbox/``. Returns the dir path."""
    preamble_dir = os.path.join(space_dir, _PREAMBLE_SUBDIR)
    os.makedirs(preamble_dir, exist_ok=True)
    this_dir = os.path.dirname(os.path.abspath(__file__))
    kernel_dir = os.path.dirname(this_dir)
    shutil.copyfile(
        os.path.join(kernel_dir, _PREAMBLE_FILENAME),
        os.path.join(preamble_dir, _PREAMBLE_FILENAME),
    )
    shutil.copyfile(
        os.path.join(this_dir, "_sitecustomize.py"),
        os.path.join(preamble_dir, _SITECUSTOMIZE_FILENAME),
    )
    return preamble_dir


def _snapshot_mtimes(root: str) -> dict[str, float]:
    """Walk root and return {relative_path: mtime_ns}. Skips .kernos_sandbox/."""
    snap: dict[str, float] = {}
    root_path = Path(root)
    skip_prefix = _PREAMBLE_SUBDIR + os.sep
    for dirpath, dirnames, filenames in os.walk(root):
        # Don't descend into our own sandbox artifacts
        dirnames[:] = [d for d in dirnames if d != _PREAMBLE_SUBDIR]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                rel = os.path.relpath(full, root)
            except ValueError:
                continue
            if rel.startswith(skip_prefix) or rel == _PREAMBLE_SUBDIR:
                continue
            try:
                snap[rel] = os.path.getmtime(full)
            except OSError:
                continue
    return snap


def _diff_mtimes(
    before: dict[str, float], after: dict[str, float],
) -> list[str]:
    """Return sorted relative paths that are new, removed, or modified."""
    changed: set[str] = set()
    for rel, mtime in after.items():
        if rel not in before or before[rel] != mtime:
            changed.add(rel)
    for rel in before:
        if rel not in after:
            changed.add(rel)
    return sorted(changed)


def _base_subprocess_env(
    space_dir: str, preamble_dir: str, scope: str,
) -> dict[str, str]:
    """Build the environment for the Aider subprocess.

    Starts minimal — no inherited API keys, no shell aliases. Adds only
    what Aider needs: PATH, HOME (redirected into scope), PYTHONPATH (with
    our sandbox dir first), locale, and — when scope=isolated —
    KERNOS_SCOPE_DIR so sitecustomize knows to apply the wrapper.

    Under ``scope=isolated`` the sitecustomize-installed wrapper permits
    reads (not writes) from Python runtime paths by default. That's
    sufficient for Aider's own imports. Anything else Aider needs to
    write to — a cache dir, an expected config location — must live
    inside ``space_dir`` (we set ``HOME=space_dir`` to redirect
    ``~/.aider/`` there).
    """
    env: dict[str, str] = {
        # PATH: include the current PATH so subprocess can find the Python
        # interpreter Aider's shebang resolves against. We don't use the
        # minimal native-backend PATH here because Aider is more
        # permissive by design.
        "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
        # Redirect Aider's ~/.aider/ cache inside the scope
        "HOME": space_dir,
        "PYTHONPATH": preamble_dir,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    if scope == "isolated":
        env["KERNOS_SCOPE_DIR"] = space_dir
        # No additional read-dirs beyond the default Python runtime paths;
        # reserved for future operator allow-list use.
    return env


class AiderBuilder:
    """Scoped-tier builder. Invokes the Aider CLI as a subprocess."""

    name = "aider"

    async def build(
        self,
        *,
        instance_id: str,
        space_id: str,
        code: str,
        timeout_seconds: int,
        write_file_name: str | None,
        data_dir: str,
        scope: str,
    ) -> BuildResult:
        timeout_seconds = max(1, min(MAX_TIMEOUT, timeout_seconds or DEFAULT_TIMEOUT))
        scope = (scope or "isolated").lower()

        # 0. Locate the aider binary. With aider-chat pip-installed into the
        #    Kernos venv, ``which`` resolves to the venv's bin/ entry.
        aider_bin = shutil.which("aider")
        if not aider_bin:
            return BuildResult(
                success=False,
                error=(
                    "Aider CLI not found on PATH. Ensure `aider-chat` is installed "
                    "in the Kernos environment (`pip install -e .`)."
                ),
                exit_code=-1,
            )

        # 1. Resolve credentials + model.
        cfg = _resolve_aider_config()
        if cfg.get("error"):
            return BuildResult(
                success=False,
                error=cfg["error"],
                exit_code=-1,
            )
        model = cfg["model"]
        env_updates = cfg["env_updates"]

        # 2. Space dir + sandbox artifacts.
        space_dir = str(
            Path(data_dir) / _safe_name(instance_id) / "spaces" / space_id / "files"
        )
        os.makedirs(space_dir, exist_ok=True)
        preamble_dir = _install_sandbox_artifacts(space_dir)

        # 3. Pre-invocation mtime snapshot.
        before_snapshot = _snapshot_mtimes(space_dir)

        # 4. Build env.
        env = _base_subprocess_env(space_dir, preamble_dir, scope)
        env.update(env_updates)

        # 5. Build CLI args.
        # Aider uses positional file args; when a write_file_name is given
        # we pass it as a starting file context so Aider edits that file.
        args: list[str] = [
            aider_bin,
            "--message", code,
            "--yes-always",
            "--no-git",
            "--no-auto-commits",
            "--no-pretty",
            "--no-stream",
            "--edit-format", "diff",
            "--model", model,
        ]
        if write_file_name:
            args.append(write_file_name)

        _display_name = write_file_name or "(no file)"
        logger.info(
            "AIDER_EXEC: space=%s model=%s scope=%s timeout=%d file=%s",
            space_id, model, scope, timeout_seconds, _display_name,
        )

        # 6. Run.
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    cwd=space_dir,
                    env=env,
                ),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "AIDER_TIMEOUT: space=%s timeout=%ds", space_id, timeout_seconds,
            )
            return BuildResult(
                success=False,
                error=f"Aider timed out after {timeout_seconds}s",
                exit_code=-1,
            )
        except Exception as exc:
            logger.warning("AIDER_ERROR: space=%s error=%s", space_id, exc)
            return BuildResult(
                success=False,
                error=str(exc)[:500],
                exit_code=-1,
            )

        stdout = (result.stdout or "")[:STDOUT_BUDGET]
        stderr = (result.stderr or "")[:STDERR_BUDGET]

        # 7. Post-invocation mtime diff.
        after_snapshot = _snapshot_mtimes(space_dir)
        files_modified = _diff_mtimes(before_snapshot, after_snapshot)

        logger.info(
            "AIDER_RESULT: space=%s success=%s exit_code=%d modified=%d",
            space_id, result.returncode == 0, result.returncode,
            len(files_modified),
        )

        extras: dict[str, Any] = {}
        if len(result.stdout or "") > STDOUT_BUDGET:
            extras["stdout_truncated"] = True
            extras["full_stdout_chars"] = len(result.stdout)

        return BuildResult(
            success=result.returncode == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=result.returncode,
            files_modified=files_modified,
            extra=extras,
        )
