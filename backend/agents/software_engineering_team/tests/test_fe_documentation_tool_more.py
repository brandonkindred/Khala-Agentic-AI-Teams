"""Additional coverage for frontend_code_v2_team documentation tool agent."""

from __future__ import annotations

from software_engineering_team.codegen_team.models import (
    Microtask,
    Phase,
    ReviewIssue,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from software_engineering_team.codegen_team.tool_agents.frontend.documentation import (
    agent as _documentation_agent_mod,
)
from software_engineering_team.codegen_team.tool_agents.frontend.documentation.agent import (
    MAX_RELEVANT_CODE_CHARS,
    _relevant_code_for_issue,
)


def _fe_microtask():
    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _fe_phase_input(**kwargs):
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


def _fe_review_issue(**kwargs):
    base = dict(source="documentation", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


class _StubAgent:
    def __init__(self, response, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _patch(monkeypatch, mod, response="", raise_exc=None):
    stub = _StubAgent(response, raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)
    return stub


def _agent():
    mod = _documentation_agent_mod
    a = mod.DocumentationToolAgent.__new__(mod.DocumentationToolAgent)
    a._model = None
    a.llm = None
    return a, mod


def test_fe_doc_document_microtask_no_model():
    a, _ = _agent()
    out = a.document_microtask(_fe_microtask(), {"a.ts": "x"}, "task")
    assert "no LLM" in out.summary


def test_fe_doc_document_microtask_no_code():
    a, mod = _agent()
    a._model = object()
    out = a.document_microtask(_fe_microtask(), {}, "task")
    assert "no code" in out.summary


def test_fe_doc_document_microtask_llm_failure(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, raise_exc=RuntimeError("boom"))
    out = a.document_microtask(_fe_microtask(), {"a.ts": "x"}, "task")
    assert "LLM error" in out.summary


def test_fe_doc_document_microtask_success(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(
        monkeypatch,
        mod,
        response="## FILE a.ts ##\nupdated\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
    )
    out = a.document_microtask(_fe_microtask(), {"a.ts": "x"}, "task")
    assert "a.ts" in out.files


def test_fe_doc_review_llm_failure(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
    out = a.review(_fe_phase_input(current_files={"a.ts": "code"}))
    assert "LLM error" in out.summary


def test_fe_doc_problem_solve_fixes(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(
        monkeypatch,
        mod,
        response="## FILE a.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
    )
    out = a.problem_solve(
        _fe_phase_input(
            current_files={"a.ts": "x"},
            review_issues=[
                _fe_review_issue(source="documentation", file_path="a.ts"),
                _fe_review_issue(source="tool_documentation", file_path="b.ts"),
            ],
        )
    )
    assert "2 of 2" in out.summary


def test_fe_doc_problem_solve_llm_failure(monkeypatch):
    a, mod = _agent()
    a._model = object()
    _patch(monkeypatch, mod, raise_exc=RuntimeError("boom"))
    out = a.problem_solve(
        _fe_phase_input(
            current_files={"a.ts": "x"},
            review_issues=[_fe_review_issue(source="documentation", file_path="a.ts")],
        )
    )
    assert "0 of 1" in out.summary


def test_fe_doc_relevant_code_for_issue_includes_large_file():
    issue = ReviewIssue(file_path="a.ts")
    big = "x" * (MAX_RELEVANT_CODE_CHARS + 5000)
    out = _relevant_code_for_issue(issue, {"a.ts": big})
    assert len(out) == MAX_RELEVANT_CODE_CHARS
    assert "[truncated;" in out
    assert big not in out


def test_fe_doc_relevant_code_for_issue_fallback_multifile():
    issue = ReviewIssue(file_path="missing.ts")
    files = {"a.ts": "X", "b.ts": "Y"}
    out = _relevant_code_for_issue(issue, files)
    assert "a.ts" in out
    assert "b.ts" in out


def test_fe_doc_relevant_code_for_issue_empty():
    out = _relevant_code_for_issue(ReviewIssue(), {})
    assert out == "(no code)"
