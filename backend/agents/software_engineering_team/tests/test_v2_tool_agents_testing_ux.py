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

    def __call__(self, prompt):
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
        a.llm = None
        return a, mod

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
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

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "## FILE x.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n")
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"x.ts": "old"},
                review_issues=[
                    _fe_issue(source="qa", file_path="x.ts"),
                    _fe_issue(source="testing_qa", file_path="y.ts"),
                    _fe_issue(source="tool_testing_qa", file_path="z.ts"),
                ],
            )
        )
        assert "3 of 3" in out.summary

    def test_problem_solve_llm_failure(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"x.ts": "old"},
                review_issues=[_fe_issue(source="qa", file_path="x.ts")],
            )
        )
        assert "0 of 1" in out.summary


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
        a.llm = None
        return a, mod

    def test_plan_no_model_returns_default(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert out.recommendations

    def test_plan_with_model_success(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
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

    def test_plan_with_model_llm_failure(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.plan(_fe_phase_input())
        assert "failed" in out.summary

    def test_plan_with_model_invalid_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "not json")
        out = a.plan(_fe_phase_input())
        # Falls back to default recommendations
        assert out.recommendations

    def test_plan_with_model_text_around_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        payload = 'prefix {"user_journeys": "j", "summary": "ok"} suffix'
        _patch(monkeypatch, mod, payload)
        out = a.plan(_fe_phase_input())
        assert "ok" in out.summary

    def test_review_with_text_around_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        payload = (
            'preface {"issues": [{"description": "bad", "severity": "high", '
            '"file_path": "a.tsx", "recommendation": "fix"}], "summary": "x"} tail'
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"a.tsx": "code"}))
        assert len(out.issues) == 1

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "## FILE a.tsx ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n")
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.tsx": "x"},
                review_issues=[
                    _fe_issue(source="ux", file_path="a.tsx"),
                    _fe_issue(source="ux_usability", file_path="b.tsx"),
                    _fe_issue(source="tool_ux_usability", file_path="c.tsx"),
                ],
            )
        )
        assert "3 of 3" in out.summary

    def test_problem_solve_llm_failure(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.tsx": "x"},
                review_issues=[_fe_issue(source="ux", file_path="a.tsx")],
            )
        )
        assert "0 of 1" in out.summary

    def test_review_invalid_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "not json")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []
