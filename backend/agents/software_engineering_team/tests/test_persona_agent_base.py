"""Tests for ``shared.persona_agent_base.run_structured_persona``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

import pytest

from software_engineering_team.shared.persona_agent_base import run_structured_persona


@dataclass
class _Output:
    issues: List[str] = field(default_factory=list)
    approved: bool = True


class _AgentResult:
    def __init__(self, structured_output: Any) -> None:
        self.structured_output = structured_output


class _FakeAgent:
    """Mimics a Strands ``Agent`` instance: callable, returns an object with
    a ``structured_output`` attribute."""

    def __init__(self, structured_output: Any) -> None:
        self._structured_output = structured_output

    def __call__(self, prompt: str, *, structured_output_model: type) -> _AgentResult:
        return _AgentResult(self._structured_output)


def _fallback(exc: Exception) -> _Output:
    return _Output(issues=[f"fallback: {exc}"], approved=False)


def test_returns_structured_output_when_type_matches() -> None:
    expected = _Output(issues=["ok"], approved=True)
    agent_factory = lambda *, model, system_prompt: _FakeAgent(expected)  # noqa: E731

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
    )

    assert result is expected


def test_falls_back_when_structured_output_has_wrong_type() -> None:
    agent_factory = lambda *, model, system_prompt: _FakeAgent("not the right type")  # noqa: E731

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
    )

    assert result.approved is False
    assert "Expected _Output, got str" in result.issues[0]


def test_falls_back_when_structured_output_is_none() -> None:
    agent_factory = lambda *, model, system_prompt: _FakeAgent(None)  # noqa: E731

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
    )

    assert result.approved is False
    assert "Expected _Output, got None" in result.issues[0]


def test_falls_back_when_agent_call_raises() -> None:
    class _RaisingAgent:
        def __call__(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("boom")

    agent_factory = lambda *, model, system_prompt: _RaisingAgent()  # noqa: E731

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
    )

    assert result.approved is False
    assert "fallback: boom" in result.issues[0]


def test_agent_factory_exception_routes_through_fallback() -> None:
    """Agent construction is inside the try/except, so a construction failure
    routes through fallback_factory rather than propagating — matching the
    documented postcondition of 'Never raises'."""

    def _raising_factory(*, model: Any, system_prompt: str) -> Any:
        raise ValueError("cannot build agent")

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=_raising_factory,
    )
    # fallback_factory returns approved=False
    assert result.approved is False


def test_on_success_applied_to_genuine_result() -> None:
    expected = _Output(issues=[], approved=True)
    agent_factory = lambda *, model, system_prompt: _FakeAgent(expected)  # noqa: E731

    def _finalize(result: _Output) -> _Output:
        result.approved = False  # e.g. a severity-derived override
        return result

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
        on_success=_finalize,
    )

    assert result is expected
    assert result.approved is False


def test_on_success_is_not_applied_to_fallback() -> None:
    """Regression test: a severity-derivation on_success must never run on
    the fallback path, or an empty-findings fallback (already approved=False)
    gets silently flipped back to approved=True by a naive
    'no findings => approved' rule."""

    class _RaisingAgent:
        def __call__(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("boom")

    agent_factory = lambda *, model, system_prompt: _RaisingAgent()  # noqa: E731

    def _finalize_should_not_run(result: _Output) -> _Output:
        raise AssertionError("on_success must not be called on the fallback path")

    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="do the thing",
        output_model=_Output,
        fallback_factory=_fallback,
        agent_factory=agent_factory,
        on_success=_finalize_should_not_run,
    )

    assert result.approved is False


def test_fallback_factory_exception_propagates() -> None:
    """A buggy fallback_factory that itself raises is not swallowed."""

    def _broken_fallback(exc: Exception) -> _Output:
        raise KeyError("fallback itself is broken")

    class _RaisingAgent:
        def __call__(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("boom")

    agent_factory = lambda *, model, system_prompt: _RaisingAgent()  # noqa: E731

    with pytest.raises(KeyError):
        run_structured_persona(
            model=object(),
            system_prompt="persona",
            user_prompt="do the thing",
            output_model=_Output,
            fallback_factory=_broken_fallback,
            agent_factory=agent_factory,
        )
