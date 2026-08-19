"""Two operator-facing gates that were silently wrong on the live instance.

Both are the same class of defect: a filter that looked like a safety measure
but was actually deciding, invisibly, that most of the operator's intent did not
apply. Neither failed loudly, which is why both survived so long.
"""
from __future__ import annotations

import os

import pytest

from kernos.messages.handler import (
    NAME_NUDGE_CEILING,
    NAME_NUDGE_DENSE_UNTIL,
    NAME_NUDGE_FIRST,
    reload_env_file,
    should_nudge_unnamed,
)


# --- /restart must reload the whole file, not a prefix ------------------------

def _env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_restart_reloads_every_key_not_just_the_kernos_prefix(tmp_path, monkeypatch):
    """The live symptom: `OPENAI_CODEX_MODEL` stayed stale across four restarts.

    `/restart` is `os.execv`, so the child inherits this environment and
    `load_dotenv()` overrides nothing that is already set. A prefix filter here
    therefore does not mean "reload less" — it means the restart silently
    reuses the old value while appearing to have worked.
    """
    monkeypatch.setenv("KERNOS_SOMETHING", "old")
    monkeypatch.setenv("OPENAI_CODEX_MODEL", "gpt-old")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-old")

    count, keys = reload_env_file(_env(tmp_path, "\n".join([
        "KERNOS_SOMETHING=new",
        "OPENAI_CODEX_MODEL=gpt-5.6-sol",
        "DISCORD_BOT_TOKEN=tok-new",
    ])))

    assert os.environ["KERNOS_SOMETHING"] == "new"
    assert os.environ["OPENAI_CODEX_MODEL"] == "gpt-5.6-sol", \
        "a non-KERNOS key edited in .env must actually take effect on restart"
    assert os.environ["DISCORD_BOT_TOKEN"] == "tok-new"
    assert count == 3 and set(keys) == {
        "KERNOS_SOMETHING", "OPENAI_CODEX_MODEL", "DISCORD_BOT_TOKEN"}


@pytest.mark.parametrize("line,key,value", [
    ('QUOTED="with spaces"', "QUOTED", "with spaces"),
    ("SINGLE='single quoted'", "SINGLE", "single quoted"),
    ("SPACED_KEY = padded", "SPACED_KEY", "padded"),
    ("URL=https://example.com/a=b?c=d", "URL", "https://example.com/a=b?c=d"),
    ("EMPTY=", "EMPTY", ""),
    ('ODD="unbalanced', "ODD", '"unbalanced'),
])
def test_value_parsing(tmp_path, monkeypatch, line, key, value):
    monkeypatch.delenv(key, raising=False)
    reload_env_file(_env(tmp_path, line + "\n"))
    assert os.environ[key] == value


def test_non_assignments_are_skipped_not_invented(tmp_path, monkeypatch):
    """A malformed line must never become an environment variable. Inventing
    one would be a config value nothing in the file actually declares."""
    before = dict(os.environ)
    count, keys = reload_env_file(_env(tmp_path, "\n".join([
        "# commented=out", "   ", "no_equals_sign", "BAD KEY=value",
        "export FOO=value", "=orphan",
    ])))
    assert count == 0 and keys == []
    assert {k: v for k, v in os.environ.items()} == before


def test_a_real_edit_survives_alongside_junk(tmp_path, monkeypatch):
    """The skip rules must not become a reason to drop a valid line."""
    monkeypatch.delenv("REAL_KEY", raising=False)
    count, keys = reload_env_file(_env(tmp_path, "\n".join([
        "# header", "", "BAD KEY=nope", "REAL_KEY=real", "", "# footer",
    ])))
    assert keys == ["REAL_KEY"] and count == 1
    assert os.environ["REAL_KEY"] == "real"


# --- the unnamed-agent nudge must decay and stop ------------------------------

def test_nudge_is_silent_before_the_agent_has_footing():
    for n in range(0, NAME_NUDGE_FIRST):
        assert should_nudge_unnamed(n) is False, n


def test_nudge_is_dense_while_the_question_is_new():
    for n in range(NAME_NUDGE_FIRST, NAME_NUDGE_DENSE_UNTIL + 1):
        assert should_nudge_unnamed(n) is True, n


def test_nudge_thins_out_rather_than_firing_every_turn():
    """The live instance had fired this ~400 consecutive times. An awareness
    line repeated on every turn is a fixed cost on every context window for a
    prompt the agent has already considered and declined."""
    window = range(NAME_NUDGE_DENSE_UNTIL + 1, 120)
    fired = [n for n in window if should_nudge_unnamed(n)]
    assert fired, "it must not go silent the moment the dense phase ends"
    assert len(fired) < len(window) / 10, \
        f"still firing on {len(fired)}/{len(window)} turns — that is not a decay"


def test_nudge_stops_entirely_past_the_ceiling():
    """Past here the agent has answered by not naming itself. The affordance
    stays reachable; repeating the question in every prompt does not."""
    assert should_nudge_unnamed(NAME_NUDGE_CEILING) in (True, False)   # boundary
    for n in (NAME_NUDGE_CEILING + 1, 405, 1000, 10_000):
        assert should_nudge_unnamed(n) is False, n


def test_the_live_observed_count_is_silent():
    """405 interactions, still unnamed, still nudging every turn — the state
    that prompted this fix."""
    assert should_nudge_unnamed(405) is False
