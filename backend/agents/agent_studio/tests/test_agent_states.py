"""Unit tests for :mod:`agent_studio.agent_states`."""

from __future__ import annotations

from agent_studio.agent_states import (
    DEFAULT_STATE_PROMPTS,
    STATE_LABELS,
    STATE_ORDER,
    default_agent_states,
)
from agent_studio.models import AgentState


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
