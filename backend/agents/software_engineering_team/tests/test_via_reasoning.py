"""Tests for code_review_agent.via_reasoning two-call split."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from pydantic import BaseModel

from llm_service.interface import LLMClient, LLMPermanentError, LLMSemanticExhaustionError
from software_engineering_team.code_review_agent.via_reasoning import (
    complete_validated_via_reasoning_local,
    formatting_system_prompt_with_untrusted_guard,
    run_agent_via_reasoning,
    wrap_with_analysis_delimiters,
)


class _Out(BaseModel):
    approved: bool
    summary: str


class _RecordingClient(LLMClient):
    def __init__(
        self,
        json_response: Optional[dict[str, Any]] = None,
        *,
        prose: str = "REVIEW PROSE",
        complete_error: Optional[Exception] = None,
    ) -> None:
        self._json_response = (
            json_response
            if json_response is not None
            else {
                "approved": True,
                "summary": "ok",
            }
        )
        self._prose = prose
        self._complete_error = complete_error
        self.reasoning_calls: list[dict[str, Any]] = []
        self.format_calls: list[dict[str, Any]] = []
        self.order: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        self.order.append("complete")
        self.reasoning_calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "think": think,
                "tools": tools,
            }
        )
        if self._complete_error is not None:
            raise self._complete_error
        return self._prose

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.order.append("complete_json")
        self.format_calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "think": think,
                "tools": tools,
                "kwargs": kwargs,
            }
        )
        return self._json_response


def test_wrap_delimiters_include_prose_and_random_boundary() -> None:
    wrapped = wrap_with_analysis_delimiters("hello findings")
    assert "hello findings" in wrapped
    assert "ANALYSIS" in wrapped
    assert "END ANALYSIS" in wrapped


def test_untrusted_guard_appended() -> None:
    out = formatting_system_prompt_with_untrusted_guard("Format JSON.")
    assert out.startswith("Format JSON.")
    assert "untrusted data" in out.lower()


def test_validated_via_reasoning_sequences_reason_then_format() -> None:
    client = _RecordingClient()
    result = complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="You are a reviewer. Answer in prose.",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        objective="review code chunk",
    )
    assert result.approved is True
    assert client.order == ["complete", "complete_json"]
    assert client.reasoning_calls[0]["think"] is True
    assert client.format_calls[0]["think"] is False
    assert "REVIEW PROSE" in client.format_calls[0]["prompt"]
    assert "Return {" in client.format_calls[0]["prompt"]


def test_validated_via_reasoning_honors_reasoning_think_false() -> None:
    client = _RecordingClient()
    complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="Prose only.",
        formatting_instructions="JSON shape here",
        objective="review code chunk",
        reasoning_think=False,
    )
    assert client.reasoning_calls[0]["think"] is False
    assert client.format_calls[0]["think"] is False


def test_validated_via_reasoning_step_one_failure_skips_format() -> None:
    client = _RecordingClient(complete_error=LLMPermanentError("boom"))
    with pytest.raises(LLMPermanentError, match="boom"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="review code chunk",
        )
    assert client.order == ["complete"]
    assert client.format_calls == []


@pytest.mark.parametrize("prose", ["", "   \n\t  "])
def test_validated_via_reasoning_empty_prose_skips_format(prose: str) -> None:
    """Thinking-only / empty complete() must not reach the formatter.

    Mapping's thinking-off retry only runs on LLMSemanticExhaustionError (or
    truncation). Formatting empty analysis into a valid empty-issues approval
    would skip that recovery and treat an unreviewed chunk as approved.
    """
    client = _RecordingClient(prose=prose)
    with pytest.raises(LLMSemanticExhaustionError, match="no usable assistant content"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="review code chunk",
        )
    assert client.order == ["complete"]
    assert client.format_calls == []


def test_validated_via_reasoning_empty_prose_still_notifies_on_attempt() -> None:
    """A blank reasoning complete() is still an LLM call; the observer must
    see it before LLMSemanticExhaustionError is raised so the transcript
    records the failed attempt that coordinator recovery may retry."""
    client = _RecordingClient(prose="")
    seen: list[tuple[str, str]] = []
    with pytest.raises(LLMSemanticExhaustionError, match="no usable assistant content"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="review code chunk",
            on_attempt=lambda prompt, response: seen.append((prompt, response)),
        )
    assert seen == [("Review this code", "")]
    assert client.format_calls == []


def test_validated_via_reasoning_rejects_empty_objective() -> None:
    client = _RecordingClient()
    with pytest.raises(ValueError, match="objective must be non-empty"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="   ",
        )
    assert client.order == []


def test_validated_via_reasoning_rejects_think_kwarg() -> None:
    client = _RecordingClient()
    with pytest.raises(TypeError, match="unexpected keyword argument 'think'"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="review code chunk",
            think=False,
        )
    assert client.order == []


def test_validated_via_reasoning_on_attempt_sees_reasoning_then_format() -> None:
    """on_attempt is invoked for the reasoning complete() and each formatting attempt."""
    client = _RecordingClient()
    seen: list[tuple[str, str]] = []
    complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="Prose only.",
        formatting_instructions="JSON shape here",
        objective="review code chunk",
        on_attempt=lambda prompt, response: seen.append((prompt, response)),
    )
    assert len(seen) == 2
    assert seen[0] == ("Review this code", "REVIEW PROSE")
    assert "REVIEW PROSE" in seen[1][0]
    assert "approved" in seen[1][1]


def test_validated_via_reasoning_on_attempt_exception_is_swallowed() -> None:
    """A buggy on_attempt observer must not fail the review."""
    client = _RecordingClient()

    def _boom(_prompt: str, _response: str) -> None:
        raise RuntimeError("observer boom")

    result = complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="Prose only.",
        formatting_instructions="JSON shape here",
        objective="review code chunk",
        on_attempt=_boom,
    )
    assert result.approved is True


def test_run_agent_via_reasoning_formats_via_underlying_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model exposes a backing LLMClient, call 2 uses complete_json."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    agent_calls: list[dict[str, Any]] = []
    format_calls: list[dict[str, Any]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            format_calls.append({"prompt": prompt, **kwargs})
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            agent_calls.append(kwargs)

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review")

    result = run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
    )

    assert result.summary == "via client"
    assert len(agent_calls) == 1
    assert len(format_calls) == 1
    assert format_calls[0]["think"] is False
    assert "REVIEW PROSE" in format_calls[0]["prompt"]


def test_run_agent_via_reasoning_forwards_model_max_tokens_to_format_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloned model's reserved max_tokens must reach complete_json.

    Submission passes clone the Strands model with the computed output
    reserve. Call 2 unwraps the backing client, so that pin has to be
    forwarded explicitly or the formatter ignores the budget.
    """
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    format_calls: list[dict[str, Any]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            format_calls.append({"prompt": prompt, **kwargs})
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review", max_tokens=4096)

    run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
    )

    assert format_calls[0]["max_tokens"] == 4096


def test_run_agent_via_reasoning_keeps_output_pin_on_reasoning_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive max_tokens pin on the model must survive the reasoning clone.

    When a caller has pinned max_tokens on the model, clearing it on the text
    clone would drop the advertised cap before formatting forwards it.
    """
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    agent_models: list[Any] = []
    format_calls: list[dict[str, Any]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            format_calls.append({"prompt": prompt, **kwargs})
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            agent_models.append(kwargs["model"])

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review", max_tokens=4096)

    run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
    )

    reasoning_cfg = agent_models[0].get_config()
    assert reasoning_cfg.get("max_tokens") == 4096
    assert format_calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize("prose", ["", "   \n"])
def test_run_agent_via_reasoning_empty_prose_skips_format(
    monkeypatch: pytest.MonkeyPatch, prose: str
) -> None:
    """Empty Agent reasoning output must raise before complete_json."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    format_calls: list[dict[str, Any]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            format_calls.append({"prompt": prompt, **kwargs})
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return prose

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review")

    with pytest.raises(LLMSemanticExhaustionError, match="no usable assistant content"):
        run_agent_via_reasoning(
            model=model,
            reasoning_prompt="Review this",
            reasoning_system_prompt="Prose reviewer",
            formatting_instructions='Return {"approved": bool, "summary": str}',
            parse=lambda raw: _Out.model_validate_json(raw),
        )
    assert format_calls == []


def test_run_agent_via_reasoning_empty_prose_still_notifies_on_reasoning_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_reasoning_agent must run before emptiness is rejected so the caller
    can record the blank reasoning conversation in the transcript."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    seen: list[Any] = []

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = [{"role": "user", "content": [{"text": "Review this"}]}]

        def __call__(self, prompt: str) -> str:
            return ""

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(DummyLLMClient(), agent_key="code_review")

    with pytest.raises(LLMSemanticExhaustionError, match="no usable assistant content"):
        run_agent_via_reasoning(
            model=model,
            reasoning_prompt="Review this",
            reasoning_system_prompt="Prose reviewer",
            formatting_instructions='Return {"approved": bool, "summary": str}',
            parse=lambda raw: _Out.model_validate_json(raw),
            on_reasoning_agent=lambda agent: seen.append(agent.messages),
        )
    assert len(seen) == 1


def test_run_agent_via_reasoning_omits_max_tokens_when_model_has_no_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset model max_tokens must not invent a formatter cap."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    format_calls: list[dict[str, Any]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            format_calls.append({"prompt": prompt, **kwargs})
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review")

    run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
    )

    assert "max_tokens" not in format_calls[0]


def test_run_agent_via_reasoning_second_call_has_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call 1 may attach tools; call 2 must never receive them."""
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    agent_calls: list[dict[str, Any]] = []
    sentinel_tool = {"name": "list_files"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            agent_calls.append(kwargs)

        def __call__(self, prompt: str) -> str:
            if len(agent_calls) == 1:
                return "REVIEW PROSE"
            return '{"approved": true, "summary": "ok"}'

    class _ClonableModel:
        def __init__(self) -> None:
            self.config: dict[str, Any] = {"response_format": "json"}
            self.clone_calls: list[dict[str, Any]] = []

        def clone(self, **overrides: Any) -> "_ClonableModel":
            self.clone_calls.append(overrides)
            cloned = _ClonableModel()
            cloned.config = {**self.config, **overrides}
            cloned.clone_calls = self.clone_calls
            return cloned

    model = _ClonableModel()
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    result = run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        tools=[sentinel_tool],
    )

    assert result.approved is True
    assert len(agent_calls) == 2
    assert agent_calls[0]["tools"] == [sentinel_tool]
    assert agent_calls[1]["tools"] == []
    assert agent_calls[0]["model"].config.get("response_format") == "text"
    assert agent_calls[1]["model"].config.get("response_format") == "json"


def test_run_agent_via_reasoning_none_reasoning_think_clones_with_max_think(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reasoning_think=None must resolve to think=True on the text pass model."""
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __call__(self, prompt: str) -> str:
            if "Return {" in prompt:
                return '{"approved": true, "summary": "ok"}'
            return "REVIEW PROSE"

    class _ClonableModel:
        def __init__(self) -> None:
            self.config: dict[str, Any] = {"response_format": "json"}
            self.clone_calls: list[dict[str, Any]] = []

        def clone(self, **overrides: Any) -> "_ClonableModel":
            self.clone_calls.append(overrides)
            cloned = _ClonableModel()
            cloned.config = {**self.config, **overrides}
            cloned.clone_calls = self.clone_calls
            return cloned

    model = _ClonableModel()
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        reasoning_think=None,
    )

    assert model.clone_calls[0]["think"] is True
    assert model.clone_calls[0]["response_format"] == "text"


def test_run_agent_via_reasoning_honors_reasoning_think_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reasoning_think=False must pass think=False to the text pass model clone."""
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __call__(self, prompt: str) -> str:
            if "Return {" in prompt:
                return '{"approved": true, "summary": "ok"}'
            return "REVIEW PROSE"

    class _ClonableModel:
        def __init__(self) -> None:
            self.config: dict[str, Any] = {"response_format": "json"}
            self.clone_calls: list[dict[str, Any]] = []

        def clone(self, **overrides: Any) -> "_ClonableModel":
            self.clone_calls.append(overrides)
            cloned = _ClonableModel()
            cloned.config = {**self.config, **overrides}
            cloned.clone_calls = self.clone_calls
            return cloned

    model = _ClonableModel()
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        reasoning_think=False,
    )

    assert model.clone_calls[0]["think"] is False
    assert model.clone_calls[0]["response_format"] == "text"


def test_run_agent_via_reasoning_wraps_bare_llm_client_with_get_strands_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare LLMClient without clone uses get_strands_model for the text pass."""
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    strands_calls: list[dict[str, Any]] = []

    def _fake_get_strands_model(agent_key: str, **kwargs: Any) -> Any:
        strands_calls.append({"agent_key": agent_key, **kwargs})

        class _FakeModel:
            def __init__(self) -> None:
                self.config = dict(kwargs)

        return _FakeModel()

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {"approved": True, "summary": "wrapped client"}

    monkeypatch.setattr(vr_mod, "get_strands_model", _fake_get_strands_model)
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    result = run_agent_via_reasoning(
        model=_RecordingClient(),
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
    )

    assert result.summary == "wrapped client"
    assert len(strands_calls) == 1
    assert strands_calls[0]["response_format"] == "text"
    assert strands_calls[0]["agent_key"] == "code_review"


def test_run_agent_via_reasoning_invokes_on_reasoning_agent_after_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_reasoning_agent receives the call-1 Agent after reasoning completes."""
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    captured: list[Any] = []
    agent_calls: list[Any] = []

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            agent_calls.append(self)

        def __call__(self, prompt: str) -> str:
            if "Return {" in prompt:
                return '{"approved": true, "summary": "ok"}'
            return "REASONING PROSE"

    class _ClonableModel:
        def clone(self, **overrides: Any) -> "_ClonableModel":
            cloned = _ClonableModel()
            cloned.config = overrides
            return cloned

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    def _on_reasoning_agent(agent: Any) -> None:
        captured.append(agent)

    run_agent_via_reasoning(
        model=_ClonableModel(),
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        on_reasoning_agent=_on_reasoning_agent,
    )

    assert len(captured) == 1
    assert captured[0] is agent_calls[0]
    assert len(agent_calls) == 2


def test_run_agent_via_reasoning_invokes_on_formatting_after_complete_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_formatting observes the format prompt and JSON text of call 2."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    seen: list[tuple[str, str]] = []

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review")

    result = run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        on_formatting=lambda prompt, response: seen.append((prompt, response)),
    )

    assert result.summary == "via client"
    assert len(seen) == 1
    assert "REVIEW PROSE" in seen[0][0]
    assert "via client" in seen[0][1]


def test_run_agent_via_reasoning_on_formatting_sees_raw_on_json_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete_json parse failure still notifies on_formatting with the raw body."""
    from llm_service import LLMClientModel, LLMJsonParseError
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    seen: list[tuple[str, str]] = []

    class _FailingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            raise LLMJsonParseError(
                "bad json",
                response_preview="not json",
                raw_response="not json at all",
            )

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_FailingClient(), agent_key="code_review")

    with pytest.raises(LLMJsonParseError):
        run_agent_via_reasoning(
            model=model,
            reasoning_prompt="Review this",
            reasoning_system_prompt="Prose reviewer",
            formatting_instructions='Return {"approved": bool, "summary": str}',
            parse=lambda raw: _Out.model_validate_json(raw),
            on_formatting=lambda prompt, response: seen.append((prompt, response)),
        )

    assert len(seen) == 1
    assert seen[0][1] == "not json at all"


def test_run_agent_via_reasoning_on_formatting_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A buggy on_formatting observer must not fail the review."""
    from llm_service import LLMClientModel
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.code_review_agent import via_reasoning as vr_mod

    class _RecordingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {"approved": True, "summary": "via client"}

    class _RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "REVIEW PROSE"

    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)
    model = LLMClientModel(_RecordingClient(), agent_key="code_review")

    def _boom(_prompt: str, _response: str) -> None:
        raise RuntimeError("observer boom")

    result = run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        parse=lambda raw: _Out.model_validate_json(raw),
        on_formatting=_boom,
    )
    assert result.summary == "via client"
