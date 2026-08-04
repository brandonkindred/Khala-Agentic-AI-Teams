"""Tests for the testing_qa and ux_usability frontend/backend tool agents (LLM paths)."""

from __future__ import annotations

import json


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


def _be_phase_input(**kwargs):
    from software_engineering_team.backend_code_v2_team.models import (
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
        language="python",
    )
    base.update(kwargs)
    return ToolAgentPhaseInput(**base)


def _fe_issue(**kwargs):
    from software_engineering_team.frontend_code_v2_team.models import ReviewIssue

    base = dict(source="qa", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


def _be_issue(**kwargs):
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue

    base = dict(source="qa", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


class _StubAgent:
    def __init__(self, response, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_prompt = None

    def __call__(self, prompt):
        self.last_prompt = prompt
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _patch(monkeypatch, mod, response="", raise_exc=None):
    stub = _StubAgent(response, raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)
    return stub


# ---------------------------------------------------------------------------
# Frontend testing_qa
# ---------------------------------------------------------------------------


class TestFETestingQA:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.testing_qa import (
            agent as mod,
        )

        a = mod.TestingQAToolAgent.__new__(mod.TestingQAToolAgent)
        a._model = None
        a._model_json = None
        a.llm = None
        return a, mod

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: no tests\nseverity: high\nfile_path: x.ts\nsource: qa\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nadd tests\n## END SUMMARY ##\n"
        )
        _patch(monkeypatch, mod, resp)
        out = a.review(_fe_phase_input(current_files={"x.ts": "code"}))
        assert len(out.issues) == 1

    def test_no_problem_solve_capability(self):
        """QA is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


# ---------------------------------------------------------------------------
# Frontend ux_usability
# ---------------------------------------------------------------------------


class TestFEUxUsability:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.ux_usability import (
            agent as mod,
        )

        a = mod.UxUsabilityToolAgent.__new__(mod.UxUsabilityToolAgent)
        a._model = None
        a._model_json = None
        a.llm = None
        return a, mod

    def test_plan_no_model_returns_default(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert out.recommendations

    def test_plan_with_model_success(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        payload = json.dumps(
            {
                "user_journeys": "happy: A->B->C",
                "wireframes_summary": "header, body, footer",
                "interaction_rules": "loading: spinner",
                "microcopy_guidelines": "concise, friendly",
                "summary": "UX defined",
            }
        )
        _patch(monkeypatch, mod, payload)
        out = a.plan(_fe_phase_input(task_description="build login"))
        assert "User Journeys" in out.recommendations[0]
        assert "UX defined" in out.summary

    def test_plan_prompt_uses_spec_context_not_task_description(self, monkeypatch):
        """{spec_content} must come from spec_context, not a second copy of task_description."""
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        stub = _patch(monkeypatch, mod, json.dumps({"summary": "ok"}))
        a.plan(
            _fe_phase_input(
                task_description="build login",
                spec_context="Users reset password via emailed link.",
            )
        )
        assert "Users reset password via emailed link." in stub.last_prompt
        assert stub.last_prompt.count("build login") == 1

    def test_plan_with_model_llm_failure(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.plan(_fe_phase_input())
        assert "failed" in out.summary

    def test_plan_with_model_invalid_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, "not json")
        out = a.plan(_fe_phase_input())
        # Falls back to default recommendations
        assert out.recommendations

    def test_plan_with_model_text_around_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        payload = 'prefix {"user_journeys": "j", "summary": "ok"} suffix'
        _patch(monkeypatch, mod, payload)
        out = a.plan(_fe_phase_input())
        assert "ok" in out.summary

    def test_review_with_text_around_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        payload = (
            'preface {"issues": [{"description": "bad", "severity": "high", '
            '"file_path": "a.tsx", "recommendation": "fix"}], "summary": "x"} tail'
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"a.tsx": "code"}))
        assert len(out.issues) == 1

    def test_no_problem_solve_capability(self):
        """UX/Usability is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")

    def test_review_invalid_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, "not json")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []


# ---------------------------------------------------------------------------
# Collapse onto SharedTestingQAToolAgent: per-team parity preserved
# ---------------------------------------------------------------------------


class TestTestingQACollapse:
    # These tests assert class-level attributes and MRO only, so they use
    # ``__new__`` to skip ``__init__`` (which would resolve a Strands model and
    # need an LLM). This mirrors the other tool-agent tests in this module.
    def _be_agent(self):
        from software_engineering_team.backend_code_v2_team.tool_agents.testing_qa.agent import (
            TestingQAToolAgent,
        )

        return TestingQAToolAgent.__new__(TestingQAToolAgent)

    def _fe_agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.testing_qa.agent import (
            TestingQAToolAgent,
        )

        return TestingQAToolAgent.__new__(TestingQAToolAgent)

    def test_both_subclass_shared_base(self):
        from software_engineering_team.shared.testing_qa_tool_agent import (
            SharedTestingQAToolAgent,
        )

        assert isinstance(self._be_agent(), SharedTestingQAToolAgent)
        assert isinstance(self._fe_agent(), SharedTestingQAToolAgent)

    def test_shared_attributes_are_identical(self):
        be, fe = self._be_agent(), self._fe_agent()
        for attr in (
            "name",
            "empty_label",
            "issue_source",
            "max_relevant_code_chars",
            "review_parse_mode",
            "plan_summary",
        ):
            assert getattr(be, attr) == getattr(fe, attr)
        assert be.issue_source == "qa"

    def test_neither_has_problem_solve(self):
        """QA is review-only in both stacks: fixing is the coding agent's job."""
        assert not hasattr(self._be_agent(), "problem_solve")
        assert not hasattr(self._fe_agent(), "problem_solve")
        assert not hasattr(self._be_agent(), "problem_solve_sources")
        assert not hasattr(self._fe_agent(), "problem_solve_sources")

    def test_per_team_plan_recommendations_differ(self):
        be_plan = self._be_agent().plan(_be_phase_input())
        fe_plan = self._fe_agent().plan(_fe_phase_input())
        assert "integration tests" in be_plan.recommendations[0]
        assert "e2e tests" in fe_plan.recommendations[0]

    def test_backend_injects_language_conventions_via_mro(self):
        # Backend MRO must still pick up BackendReviewToolAgent._problem_solving_kwargs.
        kwargs = self._be_agent()._problem_solving_kwargs(_be_phase_input(language="python"))
        assert "language_conventions" in kwargs
        # Frontend has no such injection.
        assert self._fe_agent()._problem_solving_kwargs(_fe_phase_input()) == {}
