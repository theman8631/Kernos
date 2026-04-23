"""Sandbox preamble — install filesystem scope wrappers.

This module is copied into the space directory at subprocess launch time.
It MUST be self-contained: no imports from the kernos package, because the
subprocess runs without the Kernos package on its PYTHONPATH.

The wrappers confine filesystem access to a configured space directory.

Honest ceiling: this catches accidental and casually-malicious filesystem
access. A determined adversary who can run Python can bypass it via ctypes,
raw syscalls, native binaries, or simply by un-patching the wrappers. Real
isolation against hostile code needs OS-level primitives (chroot, Landlock,
container). This module is a *scope* boundary against workspace spillover,
not a security boundary against hostile code.

Wrapped entry points:
  - builtins.open
  - os.open
  - os.listdir
  - os.scandir
  - os.chdir
  - pathlib.Path.open
  - pathlib.Path.iterdir

Notable gaps (knowingly out of scope):
  - os.stat / os.path.exists / Path.exists — metadata probes
  - os.remove / os.unlink / os.rename — deletion / rename paths
  - shutil.* — most call through open() and inherit protection
  - network / subprocess / ctypes — out of scope
"""
import builtins
import os
import pathlib

# Set by install_scope_wrapper(). Empty string means "scope not installed".
_SCOPE_ROOT = ""

# Originals, captured once at install time. Also used as the "installed" flag.
_orig: dict = {}


def _resolve_and_check(path_like, op: str) -> str:
    """Resolve path to absolute canonical form. Raise PermissionError if out of scope."""
    if path_like is None:
        raise PermissionError(f"Sandbox scope: {op} rejected — path is None")
    try:
        p = os.fspath(path_like)
    except TypeError:
        raise PermissionError(
            f"Sandbox scope: {op} rejected — non-path argument of type "
            f"{type(path_like).__name__}"
        )
    if isinstance(p, bytes):
        p = p.decode("utf-8", errors="replace")
    # realpath(abspath(...)) resolves symlinks and '..' segments consistently
    # with how we resolved the scope root at install time.
    abs_p = os.path.realpath(os.path.abspath(p))
    root = _SCOPE_ROOT
    if root and (abs_p == root or abs_p.startswith(root + os.sep)):
        return abs_p
    raise PermissionError(
        f"Sandbox scope: {op} rejected — {p!r} resolves outside space directory"
    )


def install_scope_wrapper(space_dir: str) -> None:
    """Install filesystem scope wrappers. Idempotent within a process."""
    global _SCOPE_ROOT
    _SCOPE_ROOT = os.path.realpath(os.path.abspath(space_dir))

    if _orig:
        # Already installed in this process. Update root; leave wrappers as-is.
        return

    _orig["builtins.open"] = builtins.open
    _orig["os.open"] = os.open
    _orig["os.listdir"] = os.listdir
    _orig["os.scandir"] = os.scandir
    _orig["os.chdir"] = os.chdir
    _orig["Path.open"] = pathlib.Path.open
    _orig["Path.iterdir"] = pathlib.Path.iterdir

    def _wrap_builtin_open(file, *args, **kwargs):
        # file can also be an existing file descriptor (int); fd-based access
        # cannot be intercepted at this layer, so we let it through.
        if isinstance(file, int):
            return _orig["builtins.open"](file, *args, **kwargs)
        _resolve_and_check(file, "open")
        return _orig["builtins.open"](file, *args, **kwargs)

    def _wrap_os_open(path, *args, **kwargs):
        _resolve_and_check(path, "os.open")
        return _orig["os.open"](path, *args, **kwargs)

    def _wrap_os_listdir(path="."):
        _resolve_and_check(path, "os.listdir")
        return _orig["os.listdir"](path)

    def _wrap_os_scandir(path="."):
        _resolve_and_check(path, "os.scandir")
        return _orig["os.scandir"](path)

    def _wrap_os_chdir(path):
        _resolve_and_check(path, "os.chdir")
        return _orig["os.chdir"](path)

    def _wrap_path_open(self, *args, **kwargs):
        _resolve_and_check(str(self), "Path.open")
        return _orig["Path.open"](self, *args, **kwargs)

    def _wrap_path_iterdir(self):
        _resolve_and_check(str(self), "Path.iterdir")
        return _orig["Path.iterdir"](self)

    builtins.open = _wrap_builtin_open
    os.open = _wrap_os_open
    os.listdir = _wrap_os_listdir
    os.scandir = _wrap_os_scandir
    os.chdir = _wrap_os_chdir
    pathlib.Path.open = _wrap_path_open
    pathlib.Path.iterdir = _wrap_path_iterdir


def uninstall_scope_wrapper() -> None:
    """Restore original filesystem entry points. Test helper."""
    global _SCOPE_ROOT
    if not _orig:
        return
    builtins.open = _orig["builtins.open"]
    os.open = _orig["os.open"]
    os.listdir = _orig["os.listdir"]
    os.scandir = _orig["os.scandir"]
    os.chdir = _orig["os.chdir"]
    pathlib.Path.open = _orig["Path.open"]
    pathlib.Path.iterdir = _orig["Path.iterdir"]
    _orig.clear()
    _SCOPE_ROOT = ""


def _set_scope_root_for_tests(root: str) -> None:
    """Unit-test helper: set the scope root without installing wrappers.

    Lets tests exercise :func:`_resolve_and_check` against a fake root
    without monkey-patching ``builtins.open`` in the test process (which
    would interfere with pytest's own file IO).
    """
    global _SCOPE_ROOT
    _SCOPE_ROOT = os.path.realpath(os.path.abspath(root)) if root else ""
