"""The PA Strands wrapper must propagate the caller's objective to telemetry.

PA agents pass a task-specific ``objective`` to ``complete_json`` / ``complete``;
the wrapper runs a Strands ``Agent``, so it must bind that objective into the
attribution context for the underlying ``llm_service`` call to record it.
"""

from __future__ import annotations

from llm_service.attribution import current_attribution

from ..shared.llm import _PAStrandsWrapper


class _CapturingAgent:
    """Stand-in Strands ``Agent`` that records the active objective when called."""

    def __init__(self) -> None:
        self.objective: str | None = None

    def __call__(self, prompt: str) -> str:
        self.objective = current_attribution().objective
        return '{"ok": 1}'


def test_complete_json_binds_objective() -> None:
    agent = _CapturingAgent()
    wrapper = _PAStrandsWrapper(agent)
    wrapper.complete_json("prompt", objective="classify user intent")
    assert agent.objective == "classify user intent"


def test_complete_binds_objective() -> None:
    agent = _CapturingAgent()
    wrapper = _PAStrandsWrapper(agent)
    wrapper.complete("prompt", objective="answer general request")
    assert agent.objective == "answer general request"


def test_no_objective_inherits_outer_context() -> None:
    from llm_service.attribution import llm_attribution

    agent = _CapturingAgent()
    wrapper = _PAStrandsWrapper(agent)
    # An empty objective must not clobber an objective bound by an outer scope.
    with llm_attribution(objective="outer"):
        wrapper.complete_json("prompt")
    assert agent.objective == "outer"
