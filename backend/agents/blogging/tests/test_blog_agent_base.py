"""Tests for _BlogAgentBase — shared constructor boilerplate for blogging agents."""

from __future__ import annotations

import pytest


def test_blog_agent_base_rejects_none_client() -> None:
    from agents.blogging.shared.agent_base import _BlogAgentBase

    with pytest.raises(AssertionError):
        _BlogAgentBase(llm_client=None)


def test_blog_agent_base_stores_client() -> None:
    from agents.blogging.shared.agent_base import _BlogAgentBase

    from llm_service import DummyLLMClient

    client = DummyLLMClient()
    base = _BlogAgentBase(llm_client=client)

    assert base._model is client


def test_ghost_writer_rejects_none_client() -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    with pytest.raises(AssertionError):
        GhostWriterElicitationAgent(llm_client=None)


def test_blog_planning_rejects_none_client() -> None:
    from agents.blogging.blog_planning_agent.agent import BlogPlanningAgent

    with pytest.raises(AssertionError):
        BlogPlanningAgent(llm_client=None)
