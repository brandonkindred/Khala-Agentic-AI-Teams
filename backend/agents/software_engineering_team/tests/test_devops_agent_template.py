"""Unit tests for devops_team._agent_template.DevOpsSingleShotAgent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest

from software_engineering_team.tests.conftest import _strands_model_double


@dataclass
class _FakeOut:
    summary: str
    derived: bool = False


def test_boilerplate_calls_helper_with_prompt_context_and_defaults(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    captured: Dict[str, Any] = {}

    def fake_complete(model, prompt, **kwargs):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return {"summary": "ok"}

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        fake_complete,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "SYSTEM PROMPT"

        def build_context(self, input_data: str) -> str:
            return f"ctx={input_data}"

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary=data.get("summary", ""))

    model = _strands_model_double()
    agent = Agent(model)
    out = agent.run("task-1")

    assert out == _FakeOut(summary="ok")
    assert captured["model"] is agent._model
    assert captured["prompt"] == "SYSTEM PROMPT" + Agent.PROMPT_SEPARATOR + "ctx=task-1"
    assert captured["kwargs"] == {"temperature": 0.1, "think": True}


def test_pre_call_short_circuits_without_llm(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    def boom(*_a, **_kw):
        raise AssertionError("complete_json_with_continuation must not be called")

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        boom,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "UNUSED"

        def pre_call(self, input_data: str) -> Optional[_FakeOut]:
            if input_data == "skip":
                return _FakeOut(summary="early")
            return None

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="should-not-reach")

    out = Agent(_strands_model_double()).run("skip")
    assert out == _FakeOut(summary="early")


def test_build_output_post_call_derives_field(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        lambda *_a, **_kw: {"errors": [{"error_type": "syntax"}], "summary": "dbg"},
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "DEBUG"

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            errors = data.get("errors") or []
            derived = bool(errors) and all(e.get("error_type") == "syntax" for e in errors)
            return _FakeOut(summary=data.get("summary", ""), derived=derived)

    out = Agent(_strands_model_double()).run("x")
    assert out == _FakeOut(summary="dbg", derived=True)


def test_none_temperature_and_think_omit_kwargs(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    captured: Dict[str, Any] = {}

    def fake_complete(model, prompt, **kwargs):
        captured["kwargs"] = kwargs
        return {"summary": "bare"}

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        fake_complete,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "DOC"
        temperature = None
        think = None

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary=data.get("summary", ""))

    out = Agent(_strands_model_double()).run("notes")
    assert out.summary == "bare"
    assert captured["kwargs"] == {}


def test_none_llm_client_raises() -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "X"

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="")

    with pytest.raises(AssertionError, match="llm_client is required"):
        Agent(None)  # type: ignore[arg-type]


def test_unimplemented_template_methods_raise() -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    agent = DevOpsSingleShotAgent(_strands_model_double())
    with pytest.raises(NotImplementedError, match="build_context"):
        agent.build_context("x")
    with pytest.raises(NotImplementedError, match="build_output"):
        agent.build_output("x", {})


def test_empty_prompt_raises(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = ""

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="")

    with pytest.raises(AssertionError, match="PROMPT must be a non-empty string"):
        Agent(_strands_model_double()).run("x")
