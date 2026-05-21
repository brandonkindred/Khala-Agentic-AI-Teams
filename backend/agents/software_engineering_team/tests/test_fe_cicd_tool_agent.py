"""Tests for frontend_code_v2_team cicd tool agent."""

from __future__ import annotations

import json


def _fe_microtask():
    from software_engineering_team.frontend_code_v2_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _fe_phase_input(**kwargs):
    from software_engineering_team.frontend_code_v2_team.models import (
        Phase,
        ToolAgentPhaseInput,
    )

    base = dict(
        phase=Phase.PLANNING,
        repo_path="/tmp",
        current_files={},
        task_title="t",
        task_description="d",
        task_id="t1",
        language="typescript",
    )
    base.update(kwargs)
    return ToolAgentPhaseInput(**base)


def _fe_tool_input():
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentInput

    return ToolAgentInput(
        microtask=_fe_microtask(),
        task_title="t",
        task_description="d",
        spec_content="",
        repo_path="/tmp",
    )


class _StubAgent:
    def __init__(self, response, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc

    def __call__(self, prompt):
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _patch(monkeypatch, mod, response="", raise_exc=None):
    stub = _StubAgent(response, raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)
    return stub


def _agent():
    from software_engineering_team.frontend_code_v2_team.tool_agents.cicd import agent as mod

    a = mod.CicdAdapterAgent.__new__(mod.CicdAdapterAgent)
    a._model = None
    a.llm = None
    return a, mod


def test_execute_and_run():
    a, _ = _agent()
    out = a.execute(_fe_tool_input())
    assert "CI/CD" in out.summary
    out = a.run(_fe_tool_input())
    assert "CI/CD" in out.summary


def test_plan_no_model():
    a, _ = _agent()
    out = a.plan(_fe_phase_input())
    assert len(out.recommendations) >= 4


def test_plan_llm_failure(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
    out = a.plan(_fe_phase_input())
    assert "failed" in out.summary


def test_plan_success(monkeypatch):
    a, mod = _agent()
    a._model = object()
    payload = json.dumps(
        {
            "ci_plan": "lint, test, build",
            "preview_env_plan": "vercel",
            "release_rollback_plan": "semver tags",
            "source_maps_error_reporting": "sentry",
            "pipeline_yaml": "name: CI",
            "summary": "configured CI/CD",
        }
    )
    _patch(monkeypatch, mod, payload)
    out = a.plan(_fe_phase_input(task_description="setup CI"))
    assert "CI Plan" in out.recommendations[0]
    assert "configured" in out.summary


def test_plan_invalid_json(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, "not json")
    out = a.plan(_fe_phase_input())
    # Falls back to single default
    assert out.recommendations


def test_plan_text_around_json(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, 'prefix {"ci_plan": "x", "summary": "ok"} suffix')
    out = a.plan(_fe_phase_input())
    assert "ok" in out.summary


def test_review_stub():
    a, _ = _agent()
    out = a.review(_fe_phase_input())
    assert "CI/CD review" in out.summary


def test_problem_solve_stub():
    a, _ = _agent()
    out = a.problem_solve(_fe_phase_input())
    assert "CI/CD" in out.summary


def test_deliver_no_model():
    a, _ = _agent()
    out = a.deliver(_fe_phase_input())
    assert "no LLM" in out.summary


def test_deliver_llm_failure(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
    out = a.deliver(_fe_phase_input())
    assert "failed" in out.summary


def test_deliver_with_pipeline_yaml(monkeypatch):
    a, mod = _agent()
    a._model = object()
    payload = json.dumps({"pipeline_yaml": "name: CI\non: push", "summary": "ok"})
    _patch(monkeypatch, mod, payload)
    out = a.deliver(_fe_phase_input())
    assert ".github/workflows/frontend.yml" in out.files


def test_deliver_without_pipeline_yaml(monkeypatch):
    a, mod = _agent()
    a._model = object()
    payload = json.dumps({"pipeline_yaml": "", "summary": "nothing"})
    _patch(monkeypatch, mod, payload)
    out = a.deliver(_fe_phase_input())
    assert "no files generated" in out.summary


def test_parse_json_invalid():
    """_parse_json returns {} for unparsable text."""
    a, _ = _agent()
    assert a._parse_json("not json") == {}
