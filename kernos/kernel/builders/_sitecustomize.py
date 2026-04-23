"""Auto-loaded sitecustomize template for external builder subprocesses.

Python imports any module named ``sitecustomize`` at interpreter startup if
it is reachable via ``sys.path`` — before user code, after the standard-library
plumbing. The adapter copies this file to ``{space_dir}/.kernos_sandbox/``
and prepends that directory to ``PYTHONPATH`` in the subprocess environment,
so the external builder (e.g. Aider) runs inside an interpreter that has
already applied the workspace scope wrapper.

Behavior:

* ``KERNOS_SCOPE_DIR`` env var set  → install the scope wrapper from
  ``sandbox_preamble.install_scope_wrapper(scope_dir, extra_read_dirs=...)``.
  Writes outside ``scope_dir`` raise ``PermissionError``; reads outside
  ``scope_dir`` are permitted only from Python runtime paths (so the
  subprocess can import its own dependencies) plus any paths listed in
  ``KERNOS_SCOPE_EXTRA_READ_DIRS`` (``os.pathsep``-separated).
* ``KERNOS_SCOPE_DIR`` unset        → no-op. This is the ``unleashed``
  scope path; the external builder runs without confinement.

The wrapper import itself deliberately swallows errors to stderr rather
than raising — a broken preamble must degrade to ``unleashed`` rather
than kill the builder process with an import error the operator can't
easily diagnose.
"""
import os

_scope_dir = os.environ.get("KERNOS_SCOPE_DIR", "").strip()
if _scope_dir:
    try:
        from sandbox_preamble import install_scope_wrapper

        _extra_raw = os.environ.get("KERNOS_SCOPE_EXTRA_READ_DIRS", "")
        _extra = [p for p in _extra_raw.split(os.pathsep) if p.strip()]
        install_scope_wrapper(_scope_dir, extra_read_dirs=_extra)
    except Exception as exc:
        import sys

        sys.stderr.write(f"KERNOS_SCOPE_WRAPPER_FAILED: {exc}\n")
