"""Tests for the install hook runner.

Covers Section 7 of the INSTALL-FOR-STOCK-CONNECTORS spec:
HookDescriptor / HookRegistry / HookRunner / HookStatusStore;
ordering + cycle detection; idempotent + key-gen rejection at
registration; runtime guard preventing hooks from generating
credential keys; shared-runner invocation from setup AND
self_update paths.
"""

from __future__ import annotations

import pytest

from kernos.setup.install_hooks import (
    ApplyResult,
    CheckResult,
    HookContext,
    HookDescriptor,
    HookPhase,
    HookRegistry,
    HookRunner,
    HookStatus,
    HookStatusStore,
    InstallHookError,
    build_default_registry,
    topological_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_hook(hook_id: str = "h", **kwargs) -> HookDescriptor:
    return HookDescriptor(
        hook_id=hook_id,
        check=lambda ctx: CheckResult(needs_apply=True),
        apply=lambda ctx: ApplyResult(success=True, message="ok"),
        **kwargs,
    )


def _failing_hook(hook_id: str = "fail", message: str = "boom") -> HookDescriptor:
    return HookDescriptor(
        hook_id=hook_id,
        check=lambda ctx: CheckResult(needs_apply=True),
        apply=lambda ctx: ApplyResult(success=False, message=message),
    )


def _skipped_hook(hook_id: str = "noop") -> HookDescriptor:
    return HookDescriptor(
        hook_id=hook_id,
        check=lambda ctx: CheckResult(needs_apply=False, status="up_to_date"),
        apply=lambda ctx: ApplyResult(
            success=True, message="should not run because check returned False"
        ),
    )


def _runner(tmp_path, registry: HookRegistry):
    status_store = HookStatusStore(tmp_path)
    audit: list[dict] = []

    def emit(entry: dict):
        audit.append(entry)

    return HookRunner(
        registry=registry,
        status_store=status_store,
        audit_emitter=emit,
    ), audit, status_store


# ---------------------------------------------------------------------------
# Registry: validation
# ---------------------------------------------------------------------------


def test_registry_rejects_non_idempotent_hook(tmp_path):
    r = HookRegistry()
    bad = HookDescriptor(
        hook_id="bad",
        check=lambda ctx: CheckResult(needs_apply=False),
        apply=lambda ctx: ApplyResult(success=True),
        idempotent=False,
    )
    with pytest.raises(InstallHookError, match="idempotent"):
        r.register(bad)


def test_registry_rejects_credential_key_generation_declaration():
    r = HookRegistry()
    naughty = HookDescriptor(
        hook_id="key_gen_attempt",
        check=lambda ctx: CheckResult(needs_apply=True),
        apply=lambda ctx: ApplyResult(success=True),
        attempts_credential_key_generation=True,
    )
    with pytest.raises(InstallHookError, match="MAY NOT"):
        r.register(naughty)


def test_registry_rejects_duplicate_hook_id():
    r = HookRegistry()
    r.register(_passing_hook("a"))
    with pytest.raises(InstallHookError, match="already registered"):
        r.register(_passing_hook("a"))


def test_registry_rejects_non_callable_check():
    r = HookRegistry()
    bad = HookDescriptor(
        hook_id="b",
        check="not callable",  # type: ignore[arg-type]
        apply=lambda ctx: ApplyResult(success=True),
    )
    with pytest.raises(InstallHookError, match="callable"):
        r.register(bad)


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def test_topological_order_honors_order_after():
    a = _passing_hook("a")
    b = _passing_hook("b", order_after=("a",))
    c = _passing_hook("c", order_after=("b",))
    ordered = topological_order([c, a, b])
    assert [d.hook_id for d in ordered] == ["a", "b", "c"]


def test_topological_order_preserves_registration_order_for_independent_hooks():
    a = _passing_hook("a")
    b = _passing_hook("b")
    c = _passing_hook("c")
    ordered = topological_order([a, b, c])
    assert [d.hook_id for d in ordered] == ["a", "b", "c"]


def test_topological_order_rejects_cycle():
    a = _passing_hook("a", order_after=("b",))
    b = _passing_hook("b", order_after=("a",))
    with pytest.raises(InstallHookError, match="cycle"):
        topological_order([a, b])


def test_topological_order_rejects_unknown_dependency():
    a = _passing_hook("a", order_after=("ghost",))
    with pytest.raises(InstallHookError, match="not registered"):
        topological_order([a])


# ---------------------------------------------------------------------------
# Runner: outcomes
# ---------------------------------------------------------------------------


def test_runner_records_success_outcome(tmp_path):
    r = HookRegistry()
    r.register(_passing_hook("ok"))
    runner, audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert report.succeeded == ("ok",)
    assert report.failed == ()
    assert report.skipped_check == ()
    status = status_store.get("ok")
    assert status is not None
    assert status.last_outcome == "success"
    assert status.consecutive_failures == 0
    assert audit[0]["outcome"] == "success"
    assert audit[0]["audit_category"] == "install.hook_executed"


def test_runner_records_failed_outcome_with_consecutive_counter(tmp_path):
    r = HookRegistry()
    r.register(_failing_hook("breaks", message="bad day"))
    runner, audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert report.failed == ("breaks",)
    assert status_store.get("breaks").consecutive_failures == 1

    # Second run increments the consecutive counter.
    runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert status_store.get("breaks").consecutive_failures == 2

    # Audit entries: one per run.
    assert len([e for e in audit if e["hook_id"] == "breaks"]) == 2
    assert all(e["outcome"] == "failed" for e in audit)


def test_runner_records_skipped_check(tmp_path):
    r = HookRegistry()
    r.register(_skipped_hook("noop"))
    runner, _audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert report.skipped_check == ("noop",)
    assert status_store.get("noop").last_outcome == "skipped_check"


def test_runner_isolates_failure_to_one_hook(tmp_path):
    """Section 7: failed hook is non-fatal; others continue."""
    r = HookRegistry()
    r.register(_passing_hook("first"))
    r.register(_failing_hook("middle", message="boom"))
    r.register(_passing_hook("last"))
    runner, _audit, _ = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert "first" in report.succeeded
    assert "middle" in report.failed
    assert "last" in report.succeeded


def test_runner_handles_check_exception(tmp_path):
    """A hook whose check() raises gets recorded as failed, others
    continue."""
    def broken_check(ctx):
        raise RuntimeError("nope")

    bad = HookDescriptor(
        hook_id="bad",
        check=broken_check,
        apply=lambda ctx: ApplyResult(success=True),
    )
    r = HookRegistry()
    r.register(bad)
    r.register(_passing_hook("ok"))
    runner, _audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert "bad" in report.failed
    assert "ok" in report.succeeded
    assert "RuntimeError" in status_store.get("bad").last_error


def test_runner_filters_by_phase(tmp_path):
    """Hooks declared with a specific phase only run when that phase
    is requested."""
    r = HookRegistry()
    r.register(_passing_hook("pre_only", phase=HookPhase.PRE_SETUP))
    r.register(_passing_hook("post_only", phase=HookPhase.POST_SETUP))
    r.register(_passing_hook("any_phase"))
    runner, _audit, _ = _runner(tmp_path, r)

    pre = runner.run(
        phase=HookPhase.PRE_SETUP, invoked_by="kernos_setup", data_dir=tmp_path
    )
    assert "pre_only" in pre.succeeded
    assert "post_only" not in pre.succeeded
    assert "any_phase" in pre.succeeded

    post = runner.run(
        phase=HookPhase.POST_SETUP, invoked_by="kernos_setup", data_dir=tmp_path
    )
    assert "post_only" in post.succeeded
    assert "pre_only" not in post.succeeded


# ---------------------------------------------------------------------------
# Credential-key generation guard (acceptance criterion 14, 18)
# ---------------------------------------------------------------------------


def test_runner_blocks_hook_attempting_credential_key_generation(tmp_path, monkeypatch):
    """A hook that calls `_resolve_key` from inside its apply gets
    refused at runtime via the thread-local guard. Refusal renders
    the hook as outcome=failed, install completes."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", "")  # ensure no env override
    from kernos.kernel import credentials_member

    naughty_data_dir = tmp_path / "naughty"
    naughty_data_dir.mkdir()

    def _apply_tries_keygen(ctx):
        # Direct call to the internal generator — should be refused
        # via refuse_credential_key_generation context.
        try:
            credentials_member._resolve_key(naughty_data_dir, "instance-1")
        except RuntimeError as exc:
            return ApplyResult(success=False, message=str(exc))
        return ApplyResult(success=True, message="should not happen")

    naughty = HookDescriptor(
        hook_id="naughty",
        check=lambda ctx: CheckResult(needs_apply=True),
        apply=_apply_tries_keygen,
    )
    r = HookRegistry()
    r.register(naughty)
    runner, _audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    assert "naughty" in report.failed
    err = status_store.get("naughty").last_error
    assert "may not generate" in err.lower() or "install hooks may not" in err.lower()


def test_credential_key_guard_is_reentrant(monkeypatch):
    """Nested refuse_credential_key_generation contexts stack; only
    when the outermost exits does normal generation become possible
    again."""
    from kernos.kernel import credentials_member as cm
    assert cm._key_generation_blocked() is None
    with cm.refuse_credential_key_generation("outer"):
        assert cm._key_generation_blocked() == "outer"
        with cm.refuse_credential_key_generation("inner"):
            assert cm._key_generation_blocked() == "inner"
        # Outer still active.
        assert cm._key_generation_blocked() == "outer"
    assert cm._key_generation_blocked() is None


# ---------------------------------------------------------------------------
# HookStatusStore atomic / round-trip
# ---------------------------------------------------------------------------


def test_hook_status_store_atomic_round_trip(tmp_path):
    store = HookStatusStore(tmp_path)
    store.record(
        HookStatus(
            hook_id="x",
            last_run_at="2026-04-26T00:00:00+00:00",
            last_outcome="success",
            last_duration_ms=42,
        )
    )
    fresh = HookStatusStore(tmp_path)
    got = fresh.get("x")
    assert got is not None
    assert got.last_outcome == "success"
    assert got.last_duration_ms == 42

    # No leftover .tmp file after atomic rename.
    install_dir = tmp_path / "install"
    assert not any(p.suffix == ".tmp" for p in install_dir.iterdir())


# ---------------------------------------------------------------------------
# build_default_registry
# ---------------------------------------------------------------------------


def test_build_default_registry_includes_shipped_hooks(tmp_path):
    r = build_default_registry()
    ids = {d.hook_id for d in r.list_hooks()}
    assert "service_state_init" in ids
    assert "credential_key_path" in ids


def test_default_registry_runs_clean_on_fresh_data_dir(tmp_path):
    r = build_default_registry()
    runner, _audit, status_store = _runner(tmp_path, r)
    report = runner.run(phase=None, invoked_by="kernos_setup", data_dir=tmp_path)
    # service_state_init creates the install dir; credential_key_path
    # finds no instance dirs (so nothing to fix → skipped_check).
    assert "service_state_init" in (report.succeeded + report.skipped_check)
    assert "credential_key_path" in (
        report.succeeded + report.skipped_check
    )
    # The install dir now exists.
    assert (tmp_path / "install").exists()


def test_default_registry_credential_key_hook_runs_after_state_init():
    """credential_key_path declares order_after=("service_state_init",)."""
    r = build_default_registry()
    ordered = topological_order(r.list_hooks())
    ids = [h.hook_id for h in ordered]
    assert ids.index("service_state_init") < ids.index("credential_key_path")
