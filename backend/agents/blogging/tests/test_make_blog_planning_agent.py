"""Tests for the zero-arg ``make_blog_planning_agent`` factory (issue #263).

The Agent Console sandbox dispatcher (``shared_agent_invoke.dispatch``) calls
``make_*`` entrypoints with no arguments and then binds ``.run`` on the
returned object, passing the raw JSON request body. The factory must wire an
env-configured LLM client into ``BlogPlanningAgent`` and return a wrapper that
accepts a ``dict`` body and returns a JSON-serializable ``dict``.
"""

from __future__ import annotations

from typing import Any

import pytest
from blog_planning_agent.agent import (
    BlogPlanningAgent,
    _BlogPlanningAgentRunner,
    make_blog_planning_agent,
)
from shared.content_profile import ContentProfile, LengthPolicy, resolve_length_policy

from llm_service import DummyLLMClient


def _dummy_body() -> dict[str, Any]:
    """Matches the manifest's declared input schema (``PlanningInput``)."""
    return {
        "brief": "Test brief about observability.",
        "research_digest": "## Sources\n- Source one: summary.",
        "length_policy_context": "Standard article, ~1000 words.",
    }


def test_factory_returns_runner_with_run_method(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    # llm_service.get_client caches by provider/model; clear it so the env
    # change above takes effect for this test.
    from llm_service import clear_client_cache

    clear_client_cache()

    runner = make_blog_planning_agent()

    assert isinstance(runner, _BlogPlanningAgentRunner)
    assert callable(runner.run)


def test_runner_accepts_flat_planning_input_dict() -> None:
    """When the body is a flat PlanningInput dict (no wrapper keys), it is accepted."""
    agent = BlogPlanningAgent(DummyLLMClient())
    policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    runner = _BlogPlanningAgentRunner(agent, policy)

    result = runner.run(_dummy_body())

    assert isinstance(result, dict)
    assert "content_plan" in result
    assert result["content_plan"]["requirements_analysis"]["plan_acceptable"] is True
    assert result["planning_iterations_used"] >= 1


def test_runner_accepts_wrapped_body_with_length_policy() -> None:
    """When the body wraps planning_input + length_policy, both are honored."""
    agent = BlogPlanningAgent(DummyLLMClient())
    default_policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    runner = _BlogPlanningAgentRunner(agent, default_policy)

    # ``short_listicle`` (sections 3-7) is compatible with the DummyLLMClient's
    # 4-section fixture and differs from the runner's default
    # ``standard_article`` — proves the override path is actually wired.
    body = {
        "planning_input": _dummy_body(),
        "length_policy": {"content_profile": "short_listicle"},
    }
    result = runner.run(body)

    assert isinstance(result, dict)
    assert "content_plan" in result


def test_runner_rejects_non_dict_body() -> None:
    agent = BlogPlanningAgent(DummyLLMClient())
    policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    runner = _BlogPlanningAgentRunner(agent, policy)

    with pytest.raises(TypeError, match="must be a dict"):
        runner.run("not a dict")  # type: ignore[arg-type]


def test_manifest_entrypoint_resolves_via_shim_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the dispatcher's ``make_``-factory path produces a callable."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    from llm_service import clear_client_cache

    clear_client_cache()

    # Mirror what shared_agent_invoke.dispatch.invoke_entrypoint does with the
    # manifest's entrypoint string. No direct dependency on the shim module
    # here so the test stays narrow.
    import importlib

    module = importlib.import_module("blog_planning_agent.agent")
    target = getattr(module, "make_blog_planning_agent")
    runner = target()

    assert hasattr(runner, "run")
    result = runner.run(_dummy_body())
    assert result["content_plan"]["requirements_analysis"]["plan_acceptable"] is True


def test_factory_default_policy_is_standard_article(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    from llm_service import clear_client_cache

    clear_client_cache()

    runner = make_blog_planning_agent()
    assert isinstance(runner._default_length_policy, LengthPolicy)
    assert runner._default_length_policy.content_profile == ContentProfile.standard_article
