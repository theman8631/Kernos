"""CRB Compiler replaces Drafter v1 stub (CRB C1, AC #4 + #29 + #30).

Pins:

* The production translator and the v1 stub share the same
  call signature so the Drafter cohort wires either one identically.
* CRB module docstrings carry anti-fragmentation + future-composition
  invariants.
* CRB stays a service module — no cursor/budget patterns in its
  modules (grep-style structural pin).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import kernos.kernel.crb as crb_pkg
from kernos.kernel.cohorts.drafter.compiler_helper_stub import (
    draft_to_descriptor_candidate as stub,
)
from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate as production,
)


CRB_ROOT = Path(crb_pkg.__file__).parent


class TestSwapInCompatibility:
    """AC #4: production translator drops in for the stub."""

    def test_callable_with_a_single_draft_argument(self):
        sig_p = inspect.signature(production)
        sig_s = inspect.signature(stub)
        assert list(sig_p.parameters) == list(sig_s.parameters) == ["draft"]

    def test_returns_dict(self):
        from kernos.kernel.drafts.registry import WorkflowDraft

        draft = WorkflowDraft(
            draft_id="d-1", instance_id="inst_a",
            intent_summary="t",
            partial_spec_json={
                "triggers": [{"event_type": "tool.called"}],
                "action_sequence": [{"action_type": "mark_state"}],
                "predicate": True,
            },
        )
        out = production(draft)
        assert isinstance(out, dict)


class TestModuleDocstringInvariants:
    """AC #29 + #30: anti-fragmentation + future-composition pinned."""

    def test_crb_package_docstring_has_anti_fragmentation(self):
        doc = (crb_pkg.__doc__ or "").lower()
        assert "anti-fragmentation" in doc
        assert "shared context surfaces" in doc
        assert "parallel context model" in doc

    def test_crb_package_docstring_has_future_composition(self):
        doc = (crb_pkg.__doc__ or "").lower()
        assert "future-composition" in doc
        assert "service module" in doc
        assert "no cursor" in doc or "no independent cursor" in doc


class TestNoCohortPatterns:
    """AC #30: CRB is a service, not a cohort. Static check that CRB
    modules don't import or instantiate cursor/action_log patterns
    (those are cohort substrate)."""

    def test_no_durable_event_cursor_in_crb_modules(self):
        offenders = []
        for path in CRB_ROOT.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                # Skip docstring-style mentions.
                stripped = line.lstrip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                if "DurableEventCursor" in line:
                    offenders.append((path, lineno, line.strip()))
                if "BudgetTracker" in line:
                    offenders.append((path, lineno, line.strip()))
        assert not offenders, (
            "CRB must NOT use DurableEventCursor or BudgetTracker — "
            "those are cohort substrate. CRB is a service module.\n"
            + "\n".join(
                f"  {p.relative_to(CRB_ROOT)}:{ln}  {body}"
                for p, ln, body in offenders
            )
        )
