"""Parent-format governance artifacts, as the deleted writer actually wrote them.

`tests/fixtures/legacy_governance/` holds three files copied byte-for-byte out of
`friction_response` at `f38b31f` — one open item, and the archive plus audit row
produced by closing it. They are golden fixtures, not approximations: migration
must handle what the parent *did*, not what a hand-written approximation of the
parent looks like, and every hand-rolled fixture in the first two review rounds
was missing something real artifacts always carry (the `## Condition` section,
the human-gate marker, the manifest's `final_payload` agreement).

The builders below vary one field at a time off those goldens, so a test that
constructs an abort state differs from a valid state only in the thing under
test. A builder that silently produced an INVALID artifact would make every
abort assertion vacuous, so `assert_golden_is_accepted` pins the base case.
"""
from __future__ import annotations

import json
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "legacy_governance"

SIGNATURE = "self-review:coverage-gap"
#: The parent's own path-safe encoding of SIGNATURE.
SOURCE_FILENAME = "GOVERNANCE_self_review_coverage_gap_04ade3df6e6b.md"
ARCHIVE_FILENAME = (FIXTURES / "ARCHIVE_FILENAME").read_text().strip()
MANIFEST_FILENAME = "_governance_manifest.jsonl"
SHARED_MANIFEST_FILENAME = "_manifest.jsonl"

#: sha256("self-review:coverage-gap|2026-07-01T00:00:00+00:00")[:16] — the id the
#: parent derived before occurrence ids were persisted, and the one carried by
#: the goldens.
GOLDEN_OCCURRENCE = "c380addc84ef8f52"
GOLDEN_OPENED = "2026-07-01T00:00:00+00:00"
GOLDEN_CLOSED = "2026-07-05T00:00:00+00:00"
GOLDEN_PAYLOAD = ["kernos/a.py", "kernos/b.py"]

OPEN_ITEM = (FIXTURES / "open_item.md").read_text(encoding="utf-8")
ARCHIVE = (FIXTURES / "closed_archive.md").read_text(encoding="utf-8")
MANIFEST_ROW = json.loads(
    (FIXTURES / "closed_manifest.jsonl").read_text(encoding="utf-8").strip())


def _retarget(body: str, *, occurrence: str, payload: list, opened: str,
              last_seen: str | None = None, signature: str = SIGNATURE,
              title: str | None = None, human_gated: str | None = None) -> str:
    """Rewrite the identity fields of a golden body, leaving its shape intact."""
    out = []
    in_payload = False
    for line in body.splitlines():
        if line.startswith("## Payload"):
            in_payload = True
            out.append(line)
            out.extend(f"- {p}" for p in payload)
            continue
        if in_payload:
            if line.startswith("- "):
                continue                       # replaced above
            if line.strip() and not line.startswith("#"):
                in_payload = False             # trailing prose resumes
        if line.startswith("Occurrence:"):
            out.append(f"Occurrence: {occurrence}")
        elif line.startswith("Signature:"):
            out.append(f"Signature: {signature}")
        elif line.startswith("Opened:"):
            out.append(f"Opened: {opened}")
        elif line.startswith("Last-seen:"):
            out.append(f"Last-seen: {last_seen or opened}")
        elif line.startswith("Human-gated:") and human_gated is not None:
            out.append(f"Human-gated: {human_gated}")
        elif line.startswith("# GOVERNANCE: ") and title is not None:
            out.append(f"# GOVERNANCE: {title}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def truncate_after(body: str, marker: str) -> str:
    """Everything up to and including the line starting with ``marker``.

    Models a torn write: the header survives, the sections below it do not.
    """
    kept = []
    for line in body.splitlines():
        kept.append(line)
        if line.startswith(marker):
            break
    else:
        raise AssertionError(f"marker not present: {marker!r}")
    return "\n".join(kept) + "\n"


def friction_dir(root: pathlib.Path) -> pathlib.Path:
    p = root / "diagnostics" / "friction"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolved_dir(root: pathlib.Path) -> pathlib.Path:
    p = root / "diagnostics" / "friction_resolved"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_open_item(root, *, occurrence=GOLDEN_OCCURRENCE, payload=None,
                    opened=GOLDEN_OPENED, last_seen=None, signature=SIGNATURE,
                    filename=SOURCE_FILENAME, title=None, human_gated=None,
                    body=None) -> pathlib.Path:
    body = body if body is not None else _retarget(
        OPEN_ITEM, occurrence=occurrence,
        payload=GOLDEN_PAYLOAD if payload is None else payload,
        opened=opened, last_seen=last_seen, signature=signature,
        title=title, human_gated=human_gated)
    path = friction_dir(root) / filename
    path.write_text(body, encoding="utf-8")
    return path


def write_archive(root, *, occurrence=GOLDEN_OCCURRENCE, payload=None,
                  opened=GOLDEN_OPENED, signature=SIGNATURE,
                  filename=None, title=None, human_gated=None,
                  body=None) -> pathlib.Path:
    body = body if body is not None else _retarget(
        ARCHIVE, occurrence=occurrence,
        payload=GOLDEN_PAYLOAD if payload is None else payload,
        opened=opened, signature=signature, title=title, human_gated=human_gated)
    name = filename or ARCHIVE_FILENAME.replace(GOLDEN_OCCURRENCE, occurrence)
    path = resolved_dir(root) / name
    path.write_text(body, encoding="utf-8")
    return path


def manifest_row(*, occurrence=GOLDEN_OCCURRENCE, payload=None,
                 opened=GOLDEN_OPENED, closed=GOLDEN_CLOSED,
                 signature=SIGNATURE, **overrides) -> dict:
    row = dict(MANIFEST_ROW)
    row.update({
        "governance_txn": occurrence, "governance_signature": signature,
        "opened_iso": opened, "closed_iso": closed,
        "final_payload": list(GOLDEN_PAYLOAD if payload is None else payload),
    })
    row.update(overrides)
    return row


def append_manifest(root, *rows, filename=MANIFEST_FILENAME) -> pathlib.Path:
    path = resolved_dir(root) / filename
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def write_closure(root, *, occurrence=GOLDEN_OCCURRENCE, payload=None,
                  opened=GOLDEN_OPENED, closed=GOLDEN_CLOSED,
                  signature=SIGNATURE, filename=MANIFEST_FILENAME) -> None:
    """One committed closure: the archive AND the row that committed it."""
    write_archive(root, occurrence=occurrence, payload=payload, opened=opened,
                  signature=signature)
    append_manifest(root, manifest_row(occurrence=occurrence, payload=payload,
                                       opened=opened, closed=closed,
                                       signature=signature), filename=filename)
