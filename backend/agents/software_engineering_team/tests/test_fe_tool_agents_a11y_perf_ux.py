"""Tests for the frontend_code_v2 accessibility, performance, and ux_usability tool agents."""

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


def _fe_review_issue(**kwargs):
    from software_engineering_team.frontend_code_v2_team.models import ReviewIssue

    base = dict(source="accessibility", severity="medium", description="d", file_path="", recommendation="")
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


def _patch(monkeypatch, mod, response="{}", raise_exc=None):
    stub = _StubAgent(response, raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)


# ---------------------------------------------------------------------------
# Accessibility
# ---------------------------------------------------------------------------


class TestAccessibility:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.accessibility import (
            agent as mod,
        )

        a = mod.AccessibilityToolAgent.__new__(mod.AccessibilityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        a, _ = self._agent()
        out = a.execute(_fe_tool_input())
        assert "Accessibility execute" in out.summary

    def test_run_delegates(self):
        a, _ = self._agent()
        out = a.run(_fe_tool_input())
        assert "Accessibility execute" in out.summary

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert any("WCAG" in r for r in out.recommendations)

    def test_deliver(self):
        a, _ = self._agent()
        assert "Accessibility deliver" in a.deliver(_fe_phase_input()).summary

    def test_review_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_no_code(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "{}")
        assert "no code" in a.review(_fe_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "LLM error" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        payload = json.dumps(
            {
                "issues": [
                    {
                        "severity": "high",
                        "description": "missing alt text",
                        "file_path": "Image.tsx",
                        "recommendation": "add alt attribute",
                        "wcag_criterion": "1.1.1",
                    }
                ],
                "summary": "needs work",
            }
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"Image.tsx": "<img/>"}))
        assert len(out.issues) == 1
        assert out.issues[0].source == "accessibility"

    def test_review_handles_text_around_json(self, monkeypatch):
        """Extracts JSON object even with surrounding chatter."""
        a, mod = self._agent()
        a._model = object()
        payload = (
            "Here is the result:\n"
            '{"issues": [{"description": "x", "severity": "low"}], "summary": "ok"}\n'
            "end of message"
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert len(out.issues) == 1

    def test_review_invalid_json_returns_no_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "not json at all")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []

    def test_review_malformed_inner_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "garbage {malformed: } more")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.problem_solve(_fe_phase_input()).summary

    def test_problem_solve_no_a11y_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _fe_phase_input(review_issues=[_fe_review_issue(source="security")])
        )
        assert "No accessibility issues" in out.summary

    def test_problem_solve_fixes(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(
            monkeypatch,
            mod,
            response="## FILE a.tsx ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.tsx": "x"},
                review_issues=[
                    _fe_review_issue(source="accessibility", file_path="a.tsx"),
                    _fe_review_issue(source="tool_accessibility", file_path="b.tsx"),
                ],
            )
        )
        assert "2 of 2" in out.summary

    def test_problem_solve_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.tsx": "x"},
                review_issues=[_fe_review_issue(source="accessibility", file_path="a.tsx")],
            )
        )
        assert "0 of 1" in out.summary


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestPerformance:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.performance import (
            agent as mod,
        )

        a = mod.PerformanceToolAgent.__new__(mod.PerformanceToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute(self):
        a, _ = self._agent()
        assert "Performance execute" in a.execute(_fe_tool_input()).summary

    def test_run(self):
        a, _ = self._agent()
        assert "Performance execute" in a.run(_fe_tool_input()).summary

    def test_plan(self):
        a, _ = self._agent()
        assert a.plan(_fe_phase_input()).recommendations

    def test_deliver(self):
        a, _ = self._agent()
        assert "deliver" in a.deliver(_fe_phase_input()).summary.lower()

    def test_review_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_no_code(self):
        a, _ = self._agent()
        a._model = object()
        assert "no code" in a.review(_fe_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("e"))
        assert "LLM error" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        payload = json.dumps(
            {
                "issues": [
                    {
                        "severity": "high",
                        "description": "big bundle",
                        "file_path": "a.ts",
                        "recommendation": "lazy load",
                        "category": "bundle",
                    }
                ],
                "approved": False,
                "summary": "needs splitting",
            }
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"a.ts": "x"}))
        assert len(out.issues) == 1

    def test_review_invalid_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, "not json")
        assert a.review(_fe_phase_input(current_files={"a.ts": "x"})).issues == []

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.problem_solve(_fe_phase_input()).summary

    def test_problem_solve_no_perf_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _fe_phase_input(review_issues=[_fe_review_issue(source="security")])
        )
        assert "No performance issues" in out.summary

    def test_problem_solve_fixes(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(
            monkeypatch,
            mod,
            response="## FILE a.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.ts": "x"},
                review_issues=[_fe_review_issue(source="performance", file_path="a.ts")],
            )
        )
        assert "1 of 1" in out.summary


# ---------------------------------------------------------------------------
# UX usability
# ---------------------------------------------------------------------------


class TestUxUsability:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.ux_usability import (
            agent as mod,
        )

        a = mod.UxUsabilityToolAgent.__new__(mod.UxUsabilityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute(self):
        a, _ = self._agent()
        out = a.execute(_fe_tool_input())
        # Different stub message but contains "UX" or similar
        assert "UX" in out.summary or "usability" in out.summary.lower()

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert out.recommendations

    def test_deliver(self):
        a, _ = self._agent()
        out = a.deliver(_fe_phase_input())
        assert "deliver" in out.summary.lower() or "UX" in out.summary

    def test_review_no_model(self):
        a, _ = self._agent()
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert "skipped" in out.summary

    def test_review_no_code(self):
        a, _ = self._agent()
        a._model = object()
        out = a.review(_fe_phase_input(current_files={}))
        assert "no code" in out.summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert "LLM error" in out.summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        payload = json.dumps(
            {
                "issues": [
                    {
                        "severity": "medium",
                        "description": "confusing layout",
                        "file_path": "a.tsx",
                        "recommendation": "redesign",
                    }
                ],
                "summary": "needs redesign",
            }
        )
        _patch(monkeypatch, mod, payload)
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert len(out.issues) == 1

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        out = a.problem_solve(_fe_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_ux_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _fe_phase_input(review_issues=[_fe_review_issue(source="security")])
        )
        assert "No" in out.summary
