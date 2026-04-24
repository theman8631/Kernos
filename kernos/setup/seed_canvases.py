"""First-boot canvas seeder (SYSTEM-REFERENCE-CANVAS-SEED).

Seeds three foundational canvases using Canvas v1 primitives, no new
machinery:

- **System Reference** (team, unpinned) — seeded from ``/docs/architecture/``
  plus a kernel-tools index generated from the tool catalog.
- **My Tools** (personal, per-member) — created empty at member onboarding
  completion; auto-populated by the workspace tool-registration hook.
- **Our Procedures** (team, unpinned) — seeded with a single index page.

The runner is idempotent: it checks for each canvas by name+scope before
creating. Safe to call on every boot; no-op when everything is already
seeded. Emits ``canvas.seeded`` events and structured log lines so the
operator can inspect first-boot behavior.

Deployment-shape note: seed content for the System Reference canvas is
copied from the repo's ``docs/architecture/`` directory. This assumes a
source checkout (the canonical deployment today). On deployments where
``docs/`` is not on disk, the System Reference canvas is skipped with a
warning — the other two canvases still seed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kernos.kernel.canvas import CanvasService
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


#: Reserved owner value for the System Reference canvas — no member_id
#: collision because member IDs follow ``member:<instance>:<role>``.
SYSTEM_OWNER = "system"


#: Map ``/docs/architecture/{source}.md`` → canvas page path.
#: Keeping the source→target naming explicit prevents the "which page
#: names are canonical" question from leaking into every caller.
SYSTEM_REFERENCE_SEED_MAP: dict[str, str] = {
    "context-spaces.md": "concepts/context-spaces.md",
    "cohort-and-judgment.md": "concepts/cohorts.md",
    "safety-and-gate.md": "concepts/covenants-and-gate.md",
    "disclosure-and-messenger.md": "concepts/messenger-and-disclosure.md",
    "memory.md": "concepts/memory.md",
    "cognitive-ui.md": "concepts/cognitive-ui.md",
    "canvas.md": "concepts/canvas.md",
}


SYSTEM_REFERENCE_INDEX = """# System Reference

Kernos's self-documentation. Authoritative pages about how this system works —
context spaces, cohorts, the gate, the messenger, memory, canvases.

When a member asks about Kernos internals, the agent reads these pages rather
than reconstructing from training data. Pages under `concepts/` are snapshots of
the shipped architecture docs at first-boot time; `tools/kernel-tools.md` lists
the kernel tools registered in the catalog at seed time.

Kernos does not auto-update this canvas from repo changes. After first boot,
every page here is member-editable state like any other canvas.
"""


MY_TOOLS_INDEX = """# My Tools

Tools you've built through the workspace. Each registered tool gets its own page
describing what it does, its input schema, and notes accumulated while using it.

The agent reaches for these pages first when you ask about your tools. Edit as
usage teaches you something — the agent reads your notes alongside the descriptor
when reasoning about the tool.
"""


OUR_PROCEDURES_INDEX = """# Our Procedures

Instance-wide procedures that apply across all members. Individual member
preferences live in that member's personal procedures (through the existing
`_procedures.md` system). This canvas is for rules the whole team agrees to
operate under.

Procedures are advisory inputs to agent behavior — they do not enter the
dispatch gate's verdict system.
"""


@dataclass
class SeedResult:
    """Structured outcome of a seeding pass."""

    seeded_canvases: list[str] = field(default_factory=list)
    skipped_canvases: list[str] = field(default_factory=list)
    pages_written: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Repo root discovery
# ---------------------------------------------------------------------------


def _default_repo_root() -> Path | None:
    """Best-effort locate the repo root that contains ``docs/architecture/``.

    Walks up from this module's location. Returns None if the architecture
    docs aren't present — the caller then skips the System Reference
    canvas with a warning (see deployment-shape note in module docstring).
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "docs" / "architecture").is_dir():
            return candidate
    return None


def _read_architecture_doc(repo_root: Path, filename: str) -> tuple[str, str]:
    """Return (body, last_modified_iso). Raises FileNotFoundError if missing."""
    path = repo_root / "docs" / "architecture" / filename
    body = path.read_text(encoding="utf-8")
    import datetime as _dt
    mtime = _dt.datetime.fromtimestamp(
        path.stat().st_mtime, tz=_dt.timezone.utc,
    ).isoformat()
    return body, mtime


def _render_kernel_tools_index(tool_catalog: Any) -> str:
    """Produce a markdown list of currently-registered kernel tools.

    ``tool_catalog`` is the handler's ToolCatalog. When None or empty, we
    return a short placeholder so the page still exists and can be edited.
    """
    header = "# Kernel Tools\n\nAuto-generated index of the kernel tools registered at seed time.\n\n"
    if tool_catalog is None:
        return header + "_(tool catalog not available at seed time)_\n"
    entries: list[tuple[str, str]] = []
    try:
        for entry in getattr(tool_catalog, "list_all", lambda: [])():
            name = getattr(entry, "name", None) or (entry.get("name") if isinstance(entry, dict) else None)
            desc = getattr(entry, "description", None) or (entry.get("description") if isinstance(entry, dict) else "")
            if name:
                entries.append((name, (desc or "").split("\n", 1)[0]))
    except Exception as exc:
        logger.debug("SEED_TOOL_CATALOG_READ_FAILED: %s", exc)
        return header + "_(failed to read tool catalog at seed time)_\n"
    if not entries:
        return header + "_(no tools registered at seed time)_\n"
    lines = [header]
    for name, desc in sorted(entries):
        if desc:
            lines.append(f"- **`{name}`** — {desc}")
        else:
            lines.append(f"- **`{name}`**")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Seeding runner
# ---------------------------------------------------------------------------


async def seed_canvases_on_first_boot(
    instance_id: str,
    *,
    canvas_service: CanvasService,
    instance_db: Any,
    operator_member_id: str,
    repo_root: Path | None = None,
    tool_catalog: Any = None,
    event_emit: Any = None,
) -> SeedResult:
    """Idempotent canvas seeding. Safe to call on every boot.

    Creates the System Reference and Our Procedures canvases (team scope,
    unpinned) and registers their ``canvas.seeded`` events. My Tools is
    per-member and is handled by :func:`seed_my_tools_canvas_for_member`
    when a member finishes onboarding — not at boot time.

    Parameters
    ----------
    instance_id
        The instance to seed into.
    canvas_service
        Live :class:`CanvasService` (typically via ``handler._get_canvas_service()``).
    instance_db
        The :class:`InstanceDB` (for existence checks + operator lookup).
    operator_member_id
        Resolved operator member_id (used as owner for Our Procedures).
    repo_root
        Optional override for repo root location; defaults to auto-detection.
    tool_catalog
        Optional handler tool catalog; used to render ``tools/kernel-tools.md``.
    event_emit
        Optional ``async (instance_id, event_type, payload)`` for event stream.
    """
    result = SeedResult()

    # --- System Reference (team) ---
    sysref = await instance_db.find_canvas_by_name(
        name="System Reference", scope="team",
    )
    if sysref:
        result.skipped_canvases.append("System Reference")
        logger.info(
            "SEED_SKIP: canvas=%r already present (id=%s)",
            "System Reference", sysref.get("canvas_id"),
        )
    else:
        detected_root = repo_root or _default_repo_root()
        # Validate the detected_root actually contains docs/architecture/.
        # An explicit repo_root may have been passed but be missing the docs
        # tree (non-source deployment, container image).
        if detected_root is not None and not (
            detected_root / "docs" / "architecture"
        ).is_dir():
            detected_root = None
        if detected_root is None:
            result.warnings.append(
                "System Reference canvas skipped: docs/architecture/ not on disk. "
                "This deployment shape (non-source install) does not carry the "
                "architecture reference. Seed a copy manually via page_write if needed."
            )
            logger.warning("SEED_SKIP: System Reference — docs/architecture/ not found")
        else:
            pages_written = await _seed_system_reference(
                instance_id=instance_id,
                canvas_service=canvas_service,
                operator_member_id=operator_member_id,
                repo_root=detected_root,
                tool_catalog=tool_catalog,
            )
            result.seeded_canvases.append("System Reference")
            result.pages_written += pages_written
            await _safe_emit(
                event_emit, instance_id, "canvas.seeded",
                {"name": "System Reference", "pages": pages_written},
            )
            logger.info(
                "SEED_DONE: canvas=%r pages=%d", "System Reference", pages_written,
            )

    # --- Workflow Patterns (team) ---
    # Seed order per WORKFLOW-PATTERNS-LIBRARY-AND-SEED spec:
    # System Reference → Workflow Patterns → Our Procedures.
    # The Gardener cohort (future batch) reads from this canvas as its
    # judgment-input substrate; seeding here so it's available in-instance
    # before any Gardener work lands.
    patterns = await instance_db.find_canvas_by_name(
        name="Workflow Patterns", scope="team",
    )
    if patterns:
        result.skipped_canvases.append("Workflow Patterns")
        logger.info(
            "SEED_SKIP: canvas=%r already present (id=%s)",
            "Workflow Patterns", patterns.get("canvas_id"),
        )
    else:
        wp_root = repo_root or _default_repo_root()
        # Same deployment-shape check as System Reference: if the repo's
        # docs/workflow-patterns/ directory isn't on disk, skip with a
        # structured warning rather than fail the boot.
        if wp_root is not None and not (
            wp_root / "docs" / "workflow-patterns"
        ).is_dir():
            wp_root = None
        if wp_root is None:
            result.warnings.append(
                "Workflow Patterns canvas skipped: docs/workflow-patterns/ not "
                "on disk. This deployment shape (non-source install) does not "
                "carry the pattern library. Seed a copy manually via page_write "
                "if needed."
            )
            logger.warning(
                "SEED_SKIP: Workflow Patterns — docs/workflow-patterns/ not found",
            )
        else:
            pages_written = await _seed_workflow_patterns(
                instance_id=instance_id,
                canvas_service=canvas_service,
                operator_member_id=operator_member_id,
                repo_root=wp_root,
            )
            result.seeded_canvases.append("Workflow Patterns")
            result.pages_written += pages_written
            await _safe_emit(
                event_emit, instance_id, "canvas.seeded",
                {"name": "Workflow Patterns", "pages": pages_written},
            )
            logger.info(
                "SEED_DONE: canvas=%r pages=%d", "Workflow Patterns", pages_written,
            )

    # --- Our Procedures (team) ---
    ours = await instance_db.find_canvas_by_name(
        name="Our Procedures", scope="team",
    )
    if ours:
        result.skipped_canvases.append("Our Procedures")
        logger.info(
            "SEED_SKIP: canvas=%r already present (id=%s)",
            "Our Procedures", ours.get("canvas_id"),
        )
    else:
        pages_written = await _seed_our_procedures(
            instance_id=instance_id,
            canvas_service=canvas_service,
            operator_member_id=operator_member_id,
        )
        result.seeded_canvases.append("Our Procedures")
        result.pages_written += pages_written
        await _safe_emit(
            event_emit, instance_id, "canvas.seeded",
            {"name": "Our Procedures", "pages": pages_written},
        )
        logger.info(
            "SEED_DONE: canvas=%r pages=%d", "Our Procedures", pages_written,
        )

    return result


async def seed_my_tools_canvas_for_member(
    *,
    instance_id: str,
    member_id: str,
    canvas_service: CanvasService,
    instance_db: Any,
    event_emit: Any = None,
) -> SeedResult:
    """Per-member seeder for the My Tools canvas.

    Called when a member finishes onboarding (``bootstrap_graduated=True``
    transition). Idempotent: re-calling for a member that already has a
    My Tools canvas is a no-op.
    """
    result = SeedResult()
    existing = await instance_db.find_canvas_by_name(
        name="My Tools", scope="personal", owner_member_id=member_id,
    )
    if existing:
        result.skipped_canvases.append("My Tools")
        return result

    create_result = await canvas_service.create_personal_canvas(
        instance_id=instance_id,
        member_id=member_id,
        name="My Tools",
        description="Tools you've built through the workspace.",
    )
    if not create_result.ok:
        result.warnings.append(
            f"My Tools canvas creation failed for {member_id}: {create_result.error}"
        )
        logger.warning(
            "SEED_MY_TOOLS_CREATE_FAILED: member=%s error=%s",
            member_id, create_result.error,
        )
        return result

    canvas_id = create_result.canvas_id
    await canvas_service.page_write(
        instance_id=instance_id, canvas_id=canvas_id,
        page_slug="index.md", body=MY_TOOLS_INDEX,
        writer_member_id=member_id, title="My Tools",
        page_type="note", state="current",
    )
    result.seeded_canvases.append("My Tools")
    result.pages_written += 1
    await _safe_emit(
        event_emit, instance_id, "canvas.seeded",
        {"name": "My Tools", "member_id": member_id, "pages": 1},
    )
    logger.info(
        "SEED_DONE: canvas=%r member=%s pages=%d",
        "My Tools", member_id, 1,
    )
    return result


async def append_my_tools_page(
    *,
    instance_id: str,
    member_id: str,
    tool_name: str,
    descriptor: dict,
    canvas_service: CanvasService,
    instance_db: Any,
) -> bool:
    """Auto-populate a My Tools page when a workspace tool is registered.

    Returns True when a page is written, False when the canvas doesn't exist
    (member hasn't been onboarded yet) or the write fails. Never raises —
    this is a best-effort observer on the tool-registration path.
    """
    try:
        canvas = await instance_db.find_canvas_by_name(
            name="My Tools", scope="personal", owner_member_id=member_id,
        )
        if not canvas:
            return False
        canvas_id = canvas["canvas_id"]
        slug = f"tools/{tool_name}"
        body = _render_tool_page(tool_name, descriptor)
        result = await canvas_service.page_write(
            instance_id=instance_id, canvas_id=canvas_id, page_slug=slug,
            body=body, writer_member_id=member_id,
            title=tool_name, page_type="note", state="current",
        )
        return bool(result.ok)
    except Exception as exc:  # noqa: BLE001 — observer must never break registration
        logger.debug("SEED_MY_TOOLS_PAGE_WRITE_FAILED: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _seed_system_reference(
    *,
    instance_id: str,
    canvas_service: CanvasService,
    operator_member_id: str,
    repo_root: Path,
    tool_catalog: Any,
) -> int:
    """Create the System Reference canvas and write seed pages.

    Uses ``operator_member_id`` as the writer so the frontmatter
    attribution is meaningful (creator IS an existing member). The canvas
    is scoped team — every instance member sees it. We patch the owner
    field to the reserved ``system`` value after creation so ownership
    ops require operator approval (Canvas v1 Pillar 8 convention).
    """
    create_result = await canvas_service.create(
        instance_id=instance_id,
        creator_member_id=operator_member_id,
        name="System Reference",
        scope="team",
        description="Kernos self-documentation.",
        default_page_type="note",
    )
    if not create_result.ok:
        raise RuntimeError(
            f"System Reference canvas creation failed: {create_result.error}"
        )
    canvas_id = create_result.canvas_id

    # Flip owner to 'system' — reserved value, not a member_id.
    # Canvas v1 Pillar 8 convention: ownership operations on this canvas
    # require operator approval regardless of member permissions.
    await canvas_service._db.save_canvas({
        "canvas_id": canvas_id,
        "name": "System Reference",
        "description": "Kernos self-documentation.",
        "status": "active",
        "created_at": utc_now(),
        "scope": "team",
        "owner_member_id": SYSTEM_OWNER,
        "pinned_to_spaces": "[]",
        "canvas_yaml_path": "",
    })

    pages_written = 0

    # index.md
    await canvas_service.page_write(
        instance_id=instance_id, canvas_id=canvas_id,
        page_slug="index.md", body=SYSTEM_REFERENCE_INDEX,
        writer_member_id=operator_member_id, title="System Reference",
        page_type="note", state="current",
    )
    pages_written += 1

    # concepts/*.md from docs/architecture/
    for source_name, target_path in SYSTEM_REFERENCE_SEED_MAP.items():
        try:
            body, mtime = _read_architecture_doc(repo_root, source_name)
        except FileNotFoundError:
            logger.warning(
                "SEED_ARCHITECTURE_DOC_MISSING: %s — page skipped", source_name,
            )
            continue
        await canvas_service.page_write(
            instance_id=instance_id, canvas_id=canvas_id,
            page_slug=target_path, body=body,
            writer_member_id=operator_member_id,
            title=_title_from_target(target_path),
            page_type="note", state="current",
            frontmatter_overrides={
                "last_updated_by": SYSTEM_OWNER,
                "source_file": f"docs/architecture/{source_name}",
                "source_mtime": mtime,
            },
        )
        pages_written += 1

    # tools/kernel-tools.md — auto-generated from catalog
    tools_body = _render_kernel_tools_index(tool_catalog)
    await canvas_service.page_write(
        instance_id=instance_id, canvas_id=canvas_id,
        page_slug="tools/kernel-tools.md", body=tools_body,
        writer_member_id=operator_member_id, title="Kernel Tools",
        page_type="note", state="current",
        frontmatter_overrides={"last_updated_by": SYSTEM_OWNER},
    )
    pages_written += 1

    return pages_written


async def _seed_workflow_patterns(
    *,
    instance_id: str,
    canvas_service: CanvasService,
    operator_member_id: str,
    repo_root: Path,
) -> int:
    """Create the Workflow Patterns canvas and seed pattern pages.

    Reads every ``.md`` file from ``docs/workflow-patterns/`` and writes
    each as a canvas page. Files already carry YAML frontmatter (scope /
    type / pattern / consumer) — ``parse_frontmatter`` in canvas.py
    extracts it at read time; we preserve the body verbatim here and let
    ``page_write`` layer its own system fields (``last_updated``,
    ``last_updated_by``) over the authored frontmatter.

    Like System Reference: created with the operator as writer then
    owner-flipped to the reserved ``system`` value so ownership ops
    require operator approval.
    """
    create_result = await canvas_service.create(
        instance_id=instance_id,
        creator_member_id=operator_member_id,
        name="Workflow Patterns",
        scope="team",
        description="Gardener judgment-input library.",
        default_page_type="note",
    )
    if not create_result.ok:
        raise RuntimeError(
            f"Workflow Patterns canvas creation failed: {create_result.error}"
        )
    canvas_id = create_result.canvas_id

    # Flip owner to 'system' — same convention as System Reference.
    await canvas_service._db.save_canvas({
        "canvas_id": canvas_id,
        "name": "Workflow Patterns",
        "description": "Gardener judgment-input library.",
        "status": "active",
        "created_at": utc_now(),
        "scope": "team",
        "owner_member_id": SYSTEM_OWNER,
        "pinned_to_spaces": "[]",
        "canvas_yaml_path": "",
    })

    # Index page describing what lives here.
    index_body = (
        "# Workflow Patterns\n\n"
        "Gardener judgment-input library. Each page is a workflow pattern "
        "the Gardener consults at canvas creation and during continuous "
        "evolution. Members do not interact with this canvas directly; "
        "they see canvases shaped by its patterns.\n\n"
        "Pattern 00 (`library-meta`) is the Gardener contract — the three "
        "dials (charter volatility, actor count, time horizon), the six "
        "artifact types, and the cross-pattern evolution heuristics. "
        "Patterns 01-18 are domain-specific pattern definitions.\n\n"
        "This canvas is seeded from the repo's `docs/workflow-patterns/` "
        "directory on first boot. After seeding it is member-editable "
        "state; Kernos does not auto-sync from repo updates.\n"
    )
    await canvas_service.page_write(
        instance_id=instance_id, canvas_id=canvas_id,
        page_slug="index.md", body=index_body,
        writer_member_id=operator_member_id, title="Workflow Patterns",
        page_type="note", state="current",
        frontmatter_overrides={"last_updated_by": SYSTEM_OWNER},
    )
    pages_written = 1

    # Pattern files — seed in numeric order (00, 01, ..., 18).
    import datetime as _dt
    patterns_dir = repo_root / "docs" / "workflow-patterns"
    pattern_files = sorted(
        p for p in patterns_dir.glob("*.md") if p.name != "index.md"
    )
    for pf in pattern_files:
        try:
            raw = pf.read_text(encoding="utf-8")
            mtime = _dt.datetime.fromtimestamp(
                pf.stat().st_mtime, tz=_dt.timezone.utc,
            ).isoformat()
        except OSError as exc:
            logger.warning(
                "SEED_WORKFLOW_PATTERN_READ_FAILED: %s: %s", pf.name, exc,
            )
            continue

        # Split authored frontmatter from body so page_write can apply its
        # own system fields without double-fenced YAML.
        from kernos.kernel.canvas import parse_frontmatter
        authored_fm, body = parse_frontmatter(raw)

        # Preserve authored fields (scope, type, pattern, consumer) as
        # frontmatter_overrides; page_write lays last_updated / last_updated_by
        # on top. The page's ``type`` field stays whatever the author declared.
        overrides: dict = dict(authored_fm) if authored_fm else {}
        overrides.setdefault("last_updated_by", SYSTEM_OWNER)
        overrides["source_file"] = f"docs/workflow-patterns/{pf.name}"
        overrides["source_mtime"] = mtime

        await canvas_service.page_write(
            instance_id=instance_id, canvas_id=canvas_id,
            page_slug=pf.name, body=body or raw,
            writer_member_id=operator_member_id,
            title=_title_from_pattern_file(pf.name, authored_fm),
            page_type=authored_fm.get("type", "note") if authored_fm else "note",
            state="current",
            frontmatter_overrides=overrides,
        )
        pages_written += 1

    return pages_written


def _title_from_pattern_file(filename: str, fm: dict | None) -> str:
    """Prefer the authored ``pattern`` field; fall back to the filename stem."""
    if fm and fm.get("pattern"):
        return str(fm["pattern"]).replace("-", " ").title()
    return _title_from_target(filename)


async def _seed_our_procedures(
    *,
    instance_id: str,
    canvas_service: CanvasService,
    operator_member_id: str,
) -> int:
    """Create Our Procedures canvas with a single index page."""
    create_result = await canvas_service.create(
        instance_id=instance_id,
        creator_member_id=operator_member_id,
        name="Our Procedures",
        scope="team",
        description="Instance-wide procedures.",
        default_page_type="note",
    )
    if not create_result.ok:
        raise RuntimeError(
            f"Our Procedures canvas creation failed: {create_result.error}"
        )
    canvas_id = create_result.canvas_id

    await canvas_service.page_write(
        instance_id=instance_id, canvas_id=canvas_id,
        page_slug="index.md", body=OUR_PROCEDURES_INDEX,
        writer_member_id=operator_member_id, title="Our Procedures",
        page_type="note", state="current",
    )
    return 1


def _title_from_target(target_path: str) -> str:
    stem = target_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.replace("-", " ").title()


def _render_tool_page(tool_name: str, descriptor: dict) -> str:
    """Structured body for a My Tools page from a workspace descriptor."""
    import json as _json
    schema = descriptor.get("input_schema", {})
    schema_block = _json.dumps(schema, indent=2) if schema else "{}"
    lines = [
        f"# {tool_name}",
        "",
        descriptor.get("description", "").strip() or "_(no description)_",
        "",
        "## Input schema",
        "",
        "```json",
        schema_block,
        "```",
        "",
        "## Implementation",
        "",
        f"- File: `{descriptor.get('implementation', '(unknown)')}`",
        f"- Language: {descriptor.get('language', 'python')}",
        "",
        "## Declared effects",
        "",
        str(descriptor.get("effects", "_(not declared)_")),
        "",
        "## Usage notes",
        "",
        "_(usage notes accumulate here as you use the tool)_",
        "",
        "## Known limitations",
        "",
        "_(fill in as you discover them)_",
        "",
    ]
    return "\n".join(lines)


async def _safe_emit(event_emit, instance_id, event_type, payload) -> None:
    """Event emission wrapped — seeding must never break on a failed emit."""
    if event_emit is None:
        return
    try:
        await event_emit(instance_id, event_type, payload, member_id=SYSTEM_OWNER)
    except Exception as exc:  # noqa: BLE001
        logger.debug("SEED_EMIT_FAILED: %s %s", event_type, exc)
