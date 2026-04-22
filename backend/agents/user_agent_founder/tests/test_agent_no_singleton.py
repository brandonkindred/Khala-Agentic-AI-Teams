"""Regression test for #262: FounderAgent must not share state across runs.

Before the fix, ``get_founder_agent()`` cached a module-level ``FounderAgent``
whose embedded ``strands.Agent`` kept conversation history. Two concurrent
persona runs would silently contaminate each other's rationales.

These tests pin the new contract:

1. The module no longer exposes ``get_founder_agent`` — callers must construct
   ``FounderAgent()`` per run.
2. Each ``FounderAgent()`` instance owns an independent embedded Strands agent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_get_founder_agent_is_removed():
    """The legacy singleton accessor must no longer exist."""
    from user_agent_founder import agent as agent_module

    assert not hasattr(agent_module, "get_founder_agent"), (
        "get_founder_agent singleton accessor must be removed (see #262)"
    )
    assert not hasattr(agent_module, "_agent"), (
        "_agent module-level cache must be removed (see #262)"
    )


def test_founder_agent_instances_are_independent(monkeypatch):
    """Two FounderAgent() calls must produce instances with distinct Strands agents."""
    import strands

    import llm_service

    # Each call to strands.Agent(...) returns a fresh MagicMock so instance
    # identity is observable.
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: MagicMock(name="StrandsAgentStub"))
    monkeypatch.setattr(
        llm_service, "get_strands_model", lambda team_key: MagicMock(name="ModelStub")
    )

    from user_agent_founder.agent import FounderAgent

    a1 = FounderAgent()
    a2 = FounderAgent()

    assert a1 is not a2, "FounderAgent() must return a fresh instance per call"
    assert a1._agent is not a2._agent, (
        "Embedded strands.Agent must be independent per FounderAgent instance "
        "(otherwise concurrent runs share conversation history — see #262)"
    )


def test_api_main_imports_class_not_singleton():
    """The API module must import FounderAgent directly, not a singleton accessor."""
    from user_agent_founder.api import main as api_main

    assert hasattr(api_main, "FounderAgent"), "api.main must import the FounderAgent class directly"
    assert not hasattr(api_main, "get_founder_agent"), (
        "api.main must not keep a reference to the removed singleton accessor"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
