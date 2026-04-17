"""Tests for the eval scenario markdown parser."""
from pathlib import Path

from kernos.evals.scenario import parse_scenario


def test_parse_empty_text():
    scenario = parse_scenario("", file_path=Path("empty.md"))
    assert scenario.name == "empty"
    assert scenario.purpose == ""
    assert scenario.setup.fresh_instance is True
    assert scenario.setup.members == []
    assert scenario.turns == []
    assert scenario.rubrics == []


def test_parse_title_from_header():
    scenario = parse_scenario("# Hatching Arrival\n")
    assert scenario.name == "Hatching Arrival"


def test_parse_title_falls_back_to_filename():
    scenario = parse_scenario("", file_path=Path("cross_member_isolation.md"))
    assert scenario.name == "cross member isolation"


def test_parse_purpose_section():
    text = """# Test

## Purpose
This scenario tests something.
It has multiple lines.
"""
    scenario = parse_scenario(text)
    assert "This scenario tests something" in scenario.purpose
    assert "multiple lines" in scenario.purpose


def test_parse_setup_fresh_instance_false():
    text = """# Test

## Setup
fresh_instance: false
"""
    scenario = parse_scenario(text)
    assert scenario.setup.fresh_instance is False


def test_parse_setup_members():
    text = """# Test

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
    platform: telegram
    channel_id: "2000000002"
"""
    scenario = parse_scenario(text)
    assert len(scenario.setup.members) == 2
    owner = scenario.setup.members[0]
    assert owner.id == "owner"
    assert owner.display_name == "Harold"
    assert owner.role == "owner"
    assert owner.platform == "discord"
    assert owner.channel_id == "1000000001"

    emma = scenario.setup.members[1]
    assert emma.id == "emma"
    assert emma.display_name == "Emma"
    assert emma.role == "member"  # default
    assert emma.platform == "telegram"


def test_parse_turns_messages():
    text = """# Test

## Turns
1. owner@discord: Hey there!
2. emma@telegram: Hi back.
3. owner@discord: How are you doing today?
"""
    scenario = parse_scenario(text)
    assert len(scenario.turns) == 3
    assert scenario.turns[0].sender == "owner"
    assert scenario.turns[0].platform == "discord"
    assert scenario.turns[0].content == "Hey there!"
    assert scenario.turns[1].sender == "emma"
    assert scenario.turns[1].platform == "telegram"


def test_parse_turns_action():
    text = """# Test

## Turns
1. owner@discord: Time to reset.
2. action: owner wipe_member
"""
    scenario = parse_scenario(text)
    assert len(scenario.turns) == 2
    assert scenario.turns[1].action == "wipe_member"
    assert scenario.turns[1].sender == "owner"


def test_parse_observations():
    text = """# Test

## Observations
- member_profile: owner
- knowledge
- conversation_log: emma
"""
    scenario = parse_scenario(text)
    assert len(scenario.observations) == 3
    assert scenario.observations[0].kind == "member_profile"
    assert scenario.observations[0].args == {"member": "owner"}
    assert scenario.observations[1].kind == "knowledge"
    assert scenario.observations[1].args == {}
    assert scenario.observations[2].kind == "conversation_log"
    assert scenario.observations[2].args == {"member": "emma"}


def test_parse_rubrics():
    text = """# Test

## Rubrics
- The agent did not call itself "Kernos".
- The reply feels like presence, not customer service.
- By turn 3, the agent has acknowledged the person's name.
"""
    scenario = parse_scenario(text)
    assert len(scenario.rubrics) == 3
    assert 'Kernos' in scenario.rubrics[0].question
    assert "presence" in scenario.rubrics[1].question
    assert "acknowledged" in scenario.rubrics[2].question


def test_parse_full_scenario():
    text = """# Hatching Arrival

## Purpose
Fresh wipe. First message from owner. Agent should arrive without a name.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Hey, just got this set up!
2. owner@discord: How are you?

## Observations
- member_profile: owner

## Rubrics
- The agent did not call itself "Kernos" in turn 1.
- The first reply feels like presence, not customer service.
"""
    scenario = parse_scenario(text)
    assert scenario.name == "Hatching Arrival"
    assert "Fresh wipe" in scenario.purpose
    assert len(scenario.setup.members) == 1
    assert scenario.setup.members[0].role == "owner"
    assert len(scenario.turns) == 2
    assert len(scenario.observations) == 1
    assert len(scenario.rubrics) == 2


def test_parse_ignores_comment_lines():
    text = """# Test

## Setup
> this is a comment and should be ignored
fresh_instance: true
> another comment
"""
    scenario = parse_scenario(text)
    assert scenario.setup.fresh_instance is True
