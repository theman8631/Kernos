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

from kernos.evals.mechanical import validate_mechanical_rubric
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
    """Parse the Rubrics section, supporting both semantic and mechanical forms.

    Semantic (free-text, original syntax):
        - The agent declined politely.

    Mechanical (EVAL-MECHANICAL-RUBRICS, multi-line block):
        - kind: mechanical
          check: reply_does_not_contain
          turn: any
          pattern: 'mem_[a-f0-9]+'

    Mechanical rubrics are validated at parse time against the projector path
    registry — an unknown observation kind or a where-key that isn't in the
    projector schema raises a ValueError right here, so the scenario never
    loads in a broken state.
    """
    rubrics: list[Rubric] = []
    blocks = _split_rubric_blocks(text)
    for block in blocks:
        rubrics.append(_parse_rubric_block(block))
    return rubrics


def _split_rubric_blocks(text: str) -> list[list[str]]:
    """Group the Rubrics section into per-rubric line blocks.

    A new rubric starts on a line whose first non-whitespace character is
    `-`. Continuation lines (for mechanical rubrics' sub-keys like `check:`,
    `turn:`, `where:`) are indented beneath the starter. Blank lines separate
    nothing — they just get dropped.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(">") or stripped.startswith("#"):
            continue
        if _LIST_RE.match(raw):
            if current is not None:
                blocks.append(current)
            current = [raw]
        else:
            if current is not None:
                current.append(raw)
    if current is not None:
        blocks.append(current)
    return blocks


_MECH_KIND_RE = re.compile(r"^\s*kind\s*:\s*(.+?)\s*$")


def _parse_rubric_block(lines: list[str]) -> Rubric:
    """Turn one block of lines into a Rubric (semantic or mechanical)."""
    if not lines:
        return Rubric(question="")

    first = lines[0]
    m = _LIST_RE.match(first)
    header = m.group(1).strip() if m else first.strip()

    # Detect mechanical form: first line reads `- kind: mechanical`.
    kind_match = _MECH_KIND_RE.match(header)
    if kind_match and kind_match.group(1).strip().lower() == "mechanical":
        return _parse_mechanical_rubric(lines)

    # Semantic fallback: the original behaviour. Treat the first line as the
    # question; additional indented lines (rare) are joined so multi-line
    # semantic rubrics aren't silently truncated.
    extras = [l.strip() for l in lines[1:] if l.strip()]
    question = header
    if extras:
        question = " ".join([header] + extras).strip()
    return Rubric(question=question)


def _parse_mechanical_rubric(lines: list[str]) -> Rubric:
    """Parse a mechanical rubric block into a Rubric with check + params."""
    first = lines[0]
    m = _LIST_RE.match(first)
    header = m.group(1).strip() if m else first.strip()
    fields: dict[str, object] = {}
    # Include `kind: mechanical` from the header.
    _absorb_field(fields, header)
    # Walk continuation lines, detecting nested `where:` sub-dict.
    where: dict[str, object] | None = None
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("where"):
            where = {}
            # `where:` might carry inline value on the same line (rare); accept
            # `where:` followed by indented key: value pairs (common).
            _, _, inline = stripped.partition(":")
            inline = inline.strip()
            if inline:
                for k, v in _parse_inline_dict(inline).items():
                    where[k] = v
            fields["where"] = where
            continue
        if where is not None and line.startswith((" ", "\t")) and not stripped.startswith("-"):
            # Indented continuation under `where:` — a sub-key.
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = _coerce_scalar(v.strip())
            if k:
                where[k] = v
            continue
        # Regular top-level sub-key.
        where = None
        _absorb_field(fields, stripped)

    check = str(fields.get("check", "")).strip()
    params: dict[str, object] = {k: v for k, v in fields.items() if k not in ("kind",)}
    # Note: `check` is kept in params for completeness, but we hoist it to
    # its own field on the Rubric for the dispatcher.
    params.pop("check", None)

    err = validate_mechanical_rubric(check, params)
    if err:
        raise ValueError(
            f"mechanical rubric invalid: {err}\nblock was:\n"
            + "\n".join(lines)
        )

    question = _synthesize_mechanical_question(check, params)
    return Rubric(
        question=question, kind="mechanical", check=check, params=params,
    )


def _synthesize_mechanical_question(check: str, params: dict) -> str:
    """Make a human-readable line for reports and summary tables."""
    if check in ("reply_contains", "reply_does_not_contain"):
        return f"{check}(turn={params.get('turn', 'any')!r}, pattern={params.get('pattern', '')!r})"
    if check == "observation_has":
        return f"observation_has({params.get('observation', '')!r}, where={params.get('where', {})!r})"
    if check == "observation_field_equals":
        return (
            f"observation_field_equals({params.get('observation', '')!r}, "
            f"field={params.get('field', '')!r}, value={params.get('value')!r})"
        )
    if check in ("observation_absent", "observation_empty"):
        return f"{check}({params.get('observation', '')!r})"
    if check == "trace_event_fired":
        return f"trace_event_fired({params.get('event_name', '')!r})"
    if check in ("tool_called", "tool_not_called"):
        return f"{check}({params.get('tool_name', '')!r})"
    return f"mechanical:{check}"


def _absorb_field(fields: dict, stripped: str) -> None:
    """Parse `key: value` and store into `fields` with scalar coercion."""
    if ":" not in stripped:
        return
    k, _, v = stripped.partition(":")
    k = k.strip().lower()
    v = v.strip()
    if not k:
        return
    fields[k] = _coerce_scalar(v)


def _coerce_scalar(v: str) -> object:
    """Turn a raw YAML-ish scalar string into Python. Strings stay strings
    unless they parse cleanly as bool/int/None."""
    s = v.strip()
    if not s:
        return ""
    # Quoted strings — strip the quotes, keep the value verbatim.
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    # Integer.
    try:
        if s.lstrip("-").isdigit():
            return int(s)
    except ValueError:
        pass
    return s


def _parse_inline_dict(text: str) -> dict:
    """Parse a minimal `{k: v, k2: v2}` inline dict. Narrow-scope, not YAML."""
    out: dict = {}
    body = text.strip().strip("{}").strip()
    if not body:
        return out
    for part in body.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = _coerce_scalar(v.strip())
    return out
