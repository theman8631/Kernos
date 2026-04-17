"""Scenario file parser.

Scenarios are markdown files with these sections:

    # <Title>

    ## Purpose
    Free-form prose.

    ## Setup
    fresh_instance: true
    members:
      - id: owner
        display_name: Harold
        role: owner
        platform: discord
        channel_id: "1000000001"
      - id: emma
        display_name: Emma
        role: member
        platform: telegram
        channel_id: "2000000002"

    ## Turns
    1. owner@discord: Hey, just got this set up!
    2. emma@telegram: Hi there.
    3. action: owner wipe_member

    ## Observations
    - member_profile: owner
    - knowledge
    - conversation_log: owner

    ## Rubrics
    - The agent did not call itself "Kernos".
    - The first reply feels like presence, not customer service.

The format is intentionally loose. The parser is permissive: missing sections
default to empty, extra whitespace is tolerated, and comments (lines starting
with `>`) are ignored.
"""
from __future__ import annotations

import re
from pathlib import Path

from kernos.evals.types import (
    MemberSpec, Observation, Rubric, Scenario, Setup, Turn,
)


_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
_TURN_RE = re.compile(r"^\s*\d+\.\s*(.+)$")
_LIST_RE = re.compile(r"^\s*-\s*(.+)$")
# e.g. "owner@discord: Hello there"
_MESSAGE_RE = re.compile(r"^([a-zA-Z0-9_-]+)@(\w+)\s*:\s*(.*)$", re.DOTALL)
# e.g. "action: owner wipe_member [arg1=val1, arg2=val2]"
_ACTION_RE = re.compile(r"^action\s*:\s*(\S+)\s+(\w+)(?:\s+(.*))?$")


def load_scenario(path: str | Path) -> Scenario:
    """Load and parse a scenario from a markdown file."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_scenario(text, file_path=p)


def parse_scenario(text: str, file_path: Path | None = None) -> Scenario:
    """Parse scenario markdown text into a Scenario dataclass."""
    sections = _split_sections(text)

    # Title
    title = _parse_title(text)
    if not title and file_path:
        title = file_path.stem.replace("_", " ").replace("-", " ")

    scenario = Scenario(
        name=title or "untitled",
        file_path=file_path or Path("<unknown>"),
        purpose=sections.get("purpose", "").strip(),
    )

    if "setup" in sections:
        scenario.setup = _parse_setup(sections["setup"])
    if "turns" in sections:
        scenario.turns = _parse_turns(sections["turns"])
    if "observations" in sections:
        scenario.observations = _parse_observations(sections["observations"])
    if "rubrics" in sections:
        scenario.rubrics = _parse_rubrics(sections["rubrics"])

    return scenario


# --- Internal ---


def _split_sections(text: str) -> dict[str, str]:
    """Split markdown by `## ` headers. Keys are lowercased header text."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _parse_title(text: str) -> str:
    for line in text.splitlines():
        m = _TITLE_RE.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _parse_setup(text: str) -> Setup:
    """Parse Setup section.

    Format is permissive YAML-ish:
      fresh_instance: true
      members:
        - id: owner
          display_name: Harold
          ...
    """
    setup = Setup()
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith(">")]

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("fresh_instance"):
            val = stripped.split(":", 1)[1].strip().lower()
            setup.fresh_instance = val in ("true", "yes", "1")
            i += 1
            continue
        if stripped.startswith("members"):
            # Parse list of members following this line
            i += 1
            current_member: MemberSpec | None = None
            while i < len(lines):
                ml = lines[i]
                mstripped = ml.strip()
                # End of list: a non-indented, non-list line (new top-level key)
                if not ml.startswith(" ") and not ml.startswith("\t") and not mstripped.startswith("-"):
                    break
                if mstripped.startswith("-"):
                    if current_member is not None:
                        setup.members.append(current_member)
                    # Start new member; parse the rest of this line as a field
                    current_member = MemberSpec(id="")
                    rest = mstripped[1:].strip()
                    if rest:
                        _apply_member_field(current_member, rest)
                elif ":" in mstripped and current_member is not None:
                    _apply_member_field(current_member, mstripped)
                i += 1
            if current_member is not None:
                setup.members.append(current_member)
            continue
        i += 1

    return setup


def _apply_member_field(m: MemberSpec, line: str) -> None:
    if ":" not in line:
        return
    key, val = line.split(":", 1)
    key = key.strip().lower()
    val = val.strip().strip('"').strip("'")
    if key == "id":
        m.id = val
    elif key in ("display_name", "name"):
        m.display_name = val
    elif key == "role":
        m.role = val
    elif key == "platform":
        m.platform = val
    elif key in ("channel_id", "channel"):
        m.channel_id = val


def _parse_turns(text: str) -> list[Turn]:
    turns: list[Turn] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">") or stripped.startswith("#"):
            continue
        m = _TURN_RE.match(line)
        if not m:
            continue
        payload = m.group(1).strip()
        turns.append(_parse_turn_payload(payload))
    return turns


def _parse_turn_payload(payload: str) -> Turn:
    # Action turn: "action: <sender> <action_name> [args]"
    m = _ACTION_RE.match(payload)
    if m:
        sender = m.group(1)
        action = m.group(2)
        args_text = m.group(3) or ""
        args: dict = {}
        for part in args_text.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                args[k.strip()] = v.strip()
        return Turn(sender=sender, platform="", content="", action=action, action_args=args)

    # Message turn: "sender@platform: content"
    m = _MESSAGE_RE.match(payload)
    if m:
        sender = m.group(1)
        platform = m.group(2)
        content = m.group(3).strip()
        return Turn(sender=sender, platform=platform, content=content)

    # Fallback: treat the whole line as content from an unknown sender
    return Turn(sender="unknown", platform="", content=payload)


def _parse_observations(text: str) -> list[Observation]:
    obs: list[Observation] = []
    for line in text.splitlines():
        m = _LIST_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        # e.g. "member_profile: owner" or "knowledge" or "conversation_log: owner"
        if ":" in body:
            kind, rest = body.split(":", 1)
            kind = kind.strip()
            rest = rest.strip()
            args = {}
            if rest:
                # First positional arg goes to "member" since that's the common case;
                # richer arg syntax can be added later.
                args["member"] = rest
            obs.append(Observation(kind=kind, args=args, label=f"{kind}:{rest}" if rest else kind))
        else:
            obs.append(Observation(kind=body.strip(), label=body.strip()))
    return obs


def _parse_rubrics(text: str) -> list[Rubric]:
    rubrics: list[Rubric] = []
    for line in text.splitlines():
        m = _LIST_RE.match(line)
        if not m:
            continue
        q = m.group(1).strip()
        if q:
            rubrics.append(Rubric(question=q))
    return rubrics
