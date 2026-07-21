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
