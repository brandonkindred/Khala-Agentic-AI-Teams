"""Unit tests for the dependency-light ``LlmToolAgentBase`` skeleton."""

from __future__ import annotations

import os
import subprocess
import sys

from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase

# Mirrors pytest.ini's `pythonpath = agents .` so the subprocess below can
# import `software_engineering_team` the same way the test runner does.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_AGENTS_ROOT = os.path.join(_BACKEND_ROOT, "agents")

# ---------------------------------------------------------------------------
# constructor
# ---------------------------------------------------------------------------


def test_constructor_stores_llm():
    sentinel = object()
    agent = LlmToolAgentBase(llm=sentinel)
    assert agent.llm is sentinel


def test_constructor_defaults_llm_to_none():
    agent = LlmToolAgentBase()
    assert agent.llm is None


# ---------------------------------------------------------------------------
# _agent_factory (monkeypatch resolution)
# ---------------------------------------------------------------------------


class _DemoAgent(LlmToolAgentBase):
    pass


# Provide a module-level Agent symbol so _agent_factory (which resolves Agent
# from the subclass's defining module) can find and patch it.
Agent = None


def test_agent_factory_resolves_from_defining_module(monkeypatch):
    sentinel_factory = object()
    monkeypatch.setattr(
        sys.modules[_DemoAgent.__module__], "Agent", sentinel_factory, raising=False
    )

    agent = _DemoAgent()

    assert agent._agent_factory() is sentinel_factory


# ---------------------------------------------------------------------------
# dependency purity: importing this module must never pull in
# code_review_agent (a regression test, not just an assertion in prose)
# ---------------------------------------------------------------------------


def test_module_import_does_not_pull_in_code_review_agent():
    # Run in a fresh subprocess: other tests in this session may have already
    # imported code_review_agent, which would make an in-process sys.modules
    # check order-dependent and unreliable.
    script = (
        "import sys\n"
        "import software_engineering_team.shared.llm_tool_agent_base\n"
        "polluted = [n for n in sys.modules if n.endswith('code_review_agent') "
        "or '.code_review_agent.' in n]\n"
        "assert not polluted, polluted\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([_AGENTS_ROOT, _BACKEND_ROOT]))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# opt-in model resolution (parameterized recipes)
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch, fake):
    """Patch the lazy-imported resolver before constructing an opted-in agent."""
    monkeypatch.setattr("llm_service.strands_model.resolve_strands_model", fake)


def test_resolve_models_false_does_not_set_model(monkeypatch):
    calls = []

    def fake_resolve(llm, **kwargs):
        calls.append((llm, kwargs))
        return object()

    _patch_resolve(monkeypatch, fake_resolve)

    agent = LlmToolAgentBase(llm=object())

    assert not hasattr(agent, "_model")
    assert not hasattr(agent, "_model_json")
    assert calls == []


def test_review_like_resolves_text_model_without_get_strands_model_fn(monkeypatch):
    calls = []
    text_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewLike(LlmToolAgentBase):
        resolve_models = True

    llm = object()
    agent = ReviewLike(llm=llm)

    assert agent._model is text_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {"response_format": "text"}
    assert "get_strands_model_fn" not in calls[0][1]


def test_review_like_uses_json_model_resolves_second_json_model(monkeypatch):
    calls = []
    text_model = object()
    json_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model if kwargs.get("response_format") == "text" else json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewJsonLike(LlmToolAgentBase):
        resolve_models = True
        uses_json_model = True

    llm = object()
    agent = ReviewJsonLike(llm=llm)

    assert agent._model is text_model
    assert agent._model_json is json_model
    assert len(calls) == 2
    assert calls[0][1] == {"response_format": "text"}
    assert calls[1][1] == {"response_format": "json"}
    assert "get_strands_model_fn" not in calls[0][1]
    assert "get_strands_model_fn" not in calls[1][1]


def test_get_strands_model_fn_class_attr_forwards_unbound_function(monkeypatch):
    """Class-level function attrs must not be bound via self access."""
    calls = []

    def fake_get_strands_model(llm, *, response_format):
        return object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return object()

    _patch_resolve(monkeypatch, fake_resolve)

    class PlanJsonLike(LlmToolAgentBase):
        resolve_models = True
        response_format = "json"
        get_strands_model_fn = fake_get_strands_model

    llm = object()
    PlanJsonLike(llm=llm)

    assert len(calls) == 1
    forwarded = calls[0][1]["get_strands_model_fn"]
    assert forwarded is fake_get_strands_model
    assert not hasattr(forwarded, "__self__")


def test_plan_json_like_resolves_with_get_strands_model_fn(monkeypatch):
    calls = []
    json_model = object()
    sentinel_fn = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class PlanJsonLike(LlmToolAgentBase):
        resolve_models = True
        response_format = "json"
        get_strands_model_fn = sentinel_fn

    llm = object()
    agent = PlanJsonLike(llm=llm)

    assert agent._model is json_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {
        "response_format": "json",
        "get_strands_model_fn": sentinel_fn,
    }
