"""Unit tests for :mod:`agent_platform.studio.agent_states`."""

from __future__ import annotations

from agent_platform.studio.agent_states import (
    DEFAULT_STATE_PROMPTS,
    STATE_LABELS,
    STATE_ORDER,
    default_agent_states,
    normalize_agent_states,
)
from agent_platform.studio.models import AgentState


def test_state_constants_are_in_sync() -> None:
    # The three maps must share exactly the same key set (the import-time asserts
    # guard this, but pin it explicitly too).
    assert set(STATE_LABELS) == set(STATE_ORDER)
    assert set(DEFAULT_STATE_PROMPTS) == set(STATE_ORDER)
    assert STATE_ORDER == ("planning", "executing", "researching")


def test_default_agent_states_returns_three_ordered_states() -> None:
    states = default_agent_states()
    assert [s.key for s in states] == list(STATE_ORDER)
    assert all(isinstance(s, AgentState) for s in states)
    assert all(s.system_prompt.strip() for s in states)
    assert [s.label for s in states] == ["Planning", "Executing", "Researching"]


def test_default_agent_states_returns_fresh_instances() -> None:
    # A factory, not a shared constant — each call yields independent objects so
    # mutating one caller's states never affects another's.
    a = default_agent_states()
    b = default_agent_states()
    assert a is not b
    a[0].system_prompt = "MUTATED"
    assert b[0].system_prompt != "MUTATED"


def test_normalize_backfills_empty_to_all_defaults() -> None:
    states = normalize_agent_states([])
    assert [s.key for s in states] == list(STATE_ORDER)
    assert all(s.system_prompt == DEFAULT_STATE_PROMPTS[s.key] for s in states)


def test_normalize_backfills_partial_and_keeps_edits() -> None:
    # A partial list (only planning, edited) is filled out to all three; the edit
    # survives, the missing two come from defaults, order is canonical.
    partial = [AgentState(key="planning", label="Planning", system_prompt="EDITED")]
    states = normalize_agent_states(partial)
    assert [s.key for s in states] == list(STATE_ORDER)
    assert states[0].system_prompt == "EDITED"
    assert states[1].system_prompt == DEFAULT_STATE_PROMPTS["executing"]


def test_normalize_collapses_duplicates_last_wins() -> None:
    dupes = [
        AgentState(key="planning", label="Planning", system_prompt="first"),
        AgentState(key="planning", label="Planning", system_prompt="second"),
    ]
    states = normalize_agent_states(dupes)
    assert [s.key for s in states] == list(STATE_ORDER)
    assert states[0].system_prompt == "second"


def test_normalize_stamps_canonical_labels() -> None:
    # A supplied off-label state still ends up with the canonical display label.
    weird = [AgentState(key="planning", label="Totally Custom", system_prompt="x")]
    states = normalize_agent_states(weird)
    assert states[0].label == STATE_LABELS["planning"]
