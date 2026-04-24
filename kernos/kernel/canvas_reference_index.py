"""Per-canvas reference index — outbound wiki-links + inbound queries.

CANVAS-CROSS-PAGE-INDEX. Maintains a disk-backed map of explicit
``[[target-slug]]`` references between pages within a canvas so
reference-count heuristics (Pattern 00 back-reference-promotion +
Batch C's three declared-disabled reference-dependent heuristics) can
evaluate deterministically without scanning canvas bodies on every call.

Design:
  - Storage: ``data/{instance_id}/canvases/{canvas_id}/references.json``
    JSON file. Shape: ``{"outbound": {source_slug: [target_slug, ...]}}``.
    Inbound counts derived on query; rebuilding the inbound map is cheap
    enough that keeping only outbound source-of-truth avoids
    consistency bugs.
  - Link syntax: ``[[target-slug]]`` where ``target-slug`` is a canvas
    page path with or without the ``.md`` extension. ``[[specs/launch]]``
    resolves to ``specs/launch.md``. Slashes inside the token are
    preserved for nested pages.
  - On-event maintenance: ``CanvasService.page_write`` calls
    :func:`ReferenceIndex.update_page` after a successful write, before
    event emission. The next Gardener heuristic evaluation sees the
    updated index.
  - Cold-start rebuild: when ``references.json`` is missing, corrupt, or
    stale at first load, :func:`ReferenceIndex.rebuild_from_canvas`
    walks every page in the canvas and reconstructs outbound edges.
  - Non-destructive + side-effect-safe: the index never mutates canvas
    pages. All IO is JSON read/write on the dedicated file.

Not in scope for v1:
  - Content-similarity / overlap detection (future spec)
  - Fuzzy/semantic link recognition (future suggestion layer)
  - Auto-rewrite of outbound refs on rename (future; needs rename op)
  - Cross-canvas references (separate design concern)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


#: Wiki-link token regex. Captures ``[[target]]`` where ``target`` may
#: include slashes (nested pages) and hyphens but not whitespace or
#: bracket characters. Matches greedy within a single ``[[...]]`` pair.
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\s|]+?)\]\]")


def parse_wiki_links(body: str) -> list[str]:
    """Extract ``[[target-slug]]`` tokens from a page body.

    Returns a list of normalized target slugs (``.md`` extension
    stripped if present; slashes preserved for nested targets).
    Duplicates are preserved — the caller decides whether to dedupe.
    Body without any links returns an empty list.
    """
    if not body:
        return []
    out: list[str] = []
    for match in _WIKI_LINK_RE.finditer(body):
        raw = match.group(1).strip()
        if not raw:
            continue
        out.append(_normalize_slug(raw))
    return out


def wiki_link_locations(body: str, target_slug: str) -> list[int]:
    """Character offsets of each ``[[target_slug]]`` occurrence in body.

    Used by broken-reference findings to carry repair-grade provenance
    (in-body locations) — members see exactly where to fix links.
    Normalizes the target for comparison so ``[[specs/launch]]`` and
    ``[[specs/launch.md]]`` both match when ``target_slug`` is the
    canonical form.
    """
    if not body:
        return []
    target = _normalize_slug(target_slug)
    out: list[int] = []
    for match in _WIKI_LINK_RE.finditer(body):
        if _normalize_slug(match.group(1).strip()) == target:
            out.append(match.start())
    return out


def _normalize_slug(raw: str) -> str:
    """Canonicalize a wiki-link target for comparison.

    Strips a trailing ``.md`` extension so ``[[specs/a]]`` and
    ``[[specs/a.md]]`` collide into the same index key. Lowercases
    nothing — page slugs are case-sensitive on disk, so preserving
    case keeps lookups honest.
    """
    s = raw.strip()
    if s.endswith(".md"):
        s = s[:-3]
    return s


def slug_from_path(page_path: str) -> str:
    """Canvas page path → slug for indexing."""
    return _normalize_slug(page_path)


def path_from_slug(slug: str) -> str:
    """Slug → canvas page path (with ``.md``). Inverse of ``slug_from_path``."""
    s = _normalize_slug(slug)
    return s if s.endswith(".md") else f"{s}.md"


# ---------------------------------------------------------------------------
# ReferenceIndex
# ---------------------------------------------------------------------------


@dataclass
class BrokenReference:
    """One broken outbound reference — target slug not present in canvas."""
    source_slug: str          # the page carrying the broken link
    target_slug: str          # the target that doesn't exist
    count: int                # how many times this target is referenced in source
    reason: str               # "target_missing" — v1 can't distinguish rename vs delete


@dataclass
class ReferenceIndex:
    """Per-canvas outbound-reference map with on-demand inbound queries.

    Source of truth is the ``outbound`` dict. Inbound is derived lazily —
    keeping a single canonical direction avoids drift bugs where
    ``outbound`` and ``inbound`` disagree during partial updates.
    """
    canvas_id: str
    #: source_slug → list of target_slugs the source page references. List
    #: (not set) so we can count multiple ``[[same-target]]`` occurrences
    #: within one page, which matters for Pattern 01's
    #: ``three-specs-reference-the-same-deferred-item`` class of heuristic.
    outbound: dict[str, list[str]] = field(default_factory=dict)

    # ---- Maintenance -----------------------------------------------------

    def update_page(self, source_slug: str, targets: list[str]) -> None:
        """Replace the outbound edge list for ``source_slug``."""
        source = slug_from_path(source_slug)
        if targets:
            self.outbound[source] = [slug_from_path(t) for t in targets]
        else:
            self.outbound.pop(source, None)

    def forget_page(self, source_slug: str) -> None:
        """Remove all outbound edges from this source. No inbound fixup
        needed since inbound is derived at query time."""
        self.outbound.pop(slug_from_path(source_slug), None)

    # ---- Queries ---------------------------------------------------------

    def count_outbound(self, source_slug: str) -> int:
        """Total outbound references from a single source page."""
        return len(self.outbound.get(slug_from_path(source_slug), []))

    def count_inbound(self, target_slug: str) -> int:
        """Total inbound references to a target page, counting multiplicity.

        A source page that says ``[[target]] ... [[target]]`` contributes
        2 — matters for per-source-count heuristics (three specs referring
        to the same deferred item, etc.).
        """
        target = slug_from_path(target_slug)
        total = 0
        for targets in self.outbound.values():
            total += sum(1 for t in targets if t == target)
        return total

    def count_inbound_by_source(self, target_slug: str) -> dict[str, int]:
        """Per-source inbound count — {source_slug: count} for all sources
        that reference the target."""
        target = slug_from_path(target_slug)
        out: dict[str, int] = {}
        for source, targets in self.outbound.items():
            n = sum(1 for t in targets if t == target)
            if n > 0:
                out[source] = n
        return out

    def all_inbound_counts(self) -> dict[str, int]:
        """Grouped-by-target inbound counts across the canvas.

        Returned as ``{target_slug: count}``. Used by
        back-reference-promotion and canvas-wide audit sweeps.
        """
        out: dict[str, int] = {}
        for targets in self.outbound.values():
            for t in targets:
                out[t] = out.get(t, 0) + 1
        return out

    # ---- Broken-reference detection --------------------------------------

    def find_broken_references(
        self, known_page_slugs: set[str],
    ) -> list[BrokenReference]:
        """Return outbound references whose target is not in ``known_page_slugs``.

        Caller passes the current set of canvas page slugs (from
        :func:`CanvasService.page_list`). Targets NOT in that set are
        broken. Returns one BrokenReference per (source, target) pair
        with multiplicity captured on the ``count`` field.
        """
        normalized_known = {slug_from_path(s) for s in known_page_slugs}
        broken: list[BrokenReference] = []
        for source, targets in self.outbound.items():
            per_target: dict[str, int] = {}
            for t in targets:
                if t not in normalized_known:
                    per_target[t] = per_target.get(t, 0) + 1
            for target, count in per_target.items():
                broken.append(BrokenReference(
                    source_slug=source,
                    target_slug=target,
                    count=count,
                    reason="target_missing",
                ))
        return broken

    # ---- Persistence -----------------------------------------------------

    def to_json(self) -> dict:
        return {"canvas_id": self.canvas_id, "outbound": self.outbound}

    @classmethod
    def from_json(cls, data: dict) -> "ReferenceIndex":
        if not isinstance(data, dict):
            raise ValueError("ReferenceIndex JSON must be an object")
        outbound_raw = data.get("outbound", {})
        outbound: dict[str, list[str]] = {}
        if isinstance(outbound_raw, dict):
            for k, v in outbound_raw.items():
                if isinstance(v, list):
                    outbound[str(k)] = [str(t) for t in v]
        return cls(
            canvas_id=str(data.get("canvas_id", "")),
            outbound=outbound,
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def index_path(data_dir: str, instance_id: str, canvas_id: str) -> Path:
    """Canonical path for a canvas's reference-index JSON file."""
    from kernos.utils import _safe_name
    return (
        Path(data_dir) / _safe_name(instance_id) / "canvases" / canvas_id
        / "references.json"
    )


def load_or_empty(data_dir: str, instance_id: str, canvas_id: str) -> ReferenceIndex:
    """Load the index from disk. Returns an empty ReferenceIndex when the
    file is missing, unreadable, or malformed — callers decide whether to
    trigger a rebuild at that point."""
    path = index_path(data_dir, instance_id, canvas_id)
    if not path.is_file():
        return ReferenceIndex(canvas_id=canvas_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ReferenceIndex.from_json(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "CANVAS_REF_INDEX_LOAD_FAILED: canvas=%s path=%s reason=%s",
            canvas_id, path, exc,
        )
        return ReferenceIndex(canvas_id=canvas_id)


def save(
    index: ReferenceIndex, data_dir: str, instance_id: str,
) -> None:
    """Persist the index to disk atomically."""
    path = index_path(data_dir, instance_id, index.canvas_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(index.to_json(), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


async def rebuild_from_canvas(
    *,
    canvas_service: Any,
    instance_id: str,
    canvas_id: str,
) -> ReferenceIndex:
    """Walk every page in the canvas and reconstruct outbound edges.

    Called on cold-start (index file missing) or operator-triggered
    recovery. Idempotent — calling twice produces the same index.
    Does not touch canvas pages themselves.
    """
    index = ReferenceIndex(canvas_id=canvas_id)
    try:
        pages = await canvas_service.page_list(
            instance_id=instance_id, canvas_id=canvas_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CANVAS_REF_INDEX_REBUILD_LIST_FAILED: %s", exc)
        return index

    for page in pages:
        path = page.get("path", "")
        if not path:
            continue
        try:
            result = await canvas_service.page_read(
                instance_id=instance_id, canvas_id=canvas_id, page_slug=path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("CANVAS_REF_INDEX_REBUILD_READ_FAILED: %s %s", path, exc)
            continue
        if not result.ok:
            continue
        body = result.extra.get("body", "") or ""
        targets = parse_wiki_links(body)
        if targets:
            index.update_page(path, targets)
    logger.info(
        "CANVAS_REF_INDEX_REBUILT: canvas=%s sources=%d",
        canvas_id, len(index.outbound),
    )
    return index
