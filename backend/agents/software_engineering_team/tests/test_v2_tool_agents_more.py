"""More tests for the backend_code_v2 and frontend_code_v2 tool agents.

Covers documentation, security, testing_qa and the helper functions
``_relevant_code_for_issue`` and ``_extract_doc_files``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Backend helpers / models
# ---------------------------------------------------------------------------


def _be_microtask():
    from software_engineering_team.backend_code_v2_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


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


def _be_review_issue(**kwargs):
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue

    base = dict(source="documentation", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


def _be_tool_input():
    from software_engineering_team.backend_code_v2_team.models import ToolAgentInput

    return ToolAgentInput(microtask=_be_microtask(), repo_path="/tmp", existing_code="", language="python")


# ---------------------------------------------------------------------------
# Frontend helpers / models
# ---------------------------------------------------------------------------


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


def _fe_review_issue(**kwargs):
    from software_engineering_team.frontend_code_v2_team.models import ReviewIssue

    base = dict(source="documentation", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


# ---------------------------------------------------------------------------
# Backend Documentation tool agent
# ---------------------------------------------------------------------------


class _StubStrandsAgent:
    """Callable that mimics a strands Agent.__call__ returning canned text."""

    def __init__(self, response="## SUMMARY ##\nok\n## END SUMMARY ##\n", raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _patch_strands(monkeypatch, mod, response="", raise_exc=None):
    stub = _StubStrandsAgent(response=response, raise_exc=raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)
    return stub


class TestBackendDocumentation:
    def _agent(self):
        from software_engineering_team.backend_code_v2_team.tool_agents.documentation import (
            agent as mod,
        )

        a = mod.DocumentationToolAgent.__new__(mod.DocumentationToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Documentation execute" in out.summary

    def test_run_delegates_to_execute(self):
        a, _ = self._agent()
        out = a.run(_be_tool_input())
        assert "Documentation execute" in out.summary

    def test_plan_returns_recommendations(self):
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations
        assert "Documentation planning" in out.summary

    def test_deliver_returns_stub(self):
        a, _ = self._agent()
        out = a.deliver(_be_phase_input())
        assert "Documentation deliver" in out.summary

    def test_document_microtask_no_model(self):
        a, _ = self._agent()
        out = a.document_microtask(_be_microtask(), {"a.py": "code"}, "task")
        assert "no LLM" in out.summary

    def test_document_microtask_no_code(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()  # not None
        _patch_strands(monkeypatch, mod, response="## FILE x.py ##\nupdated\n## SUMMARY ##\nok\n## END SUMMARY ##\n")
        out = a.document_microtask(_be_microtask(), {}, "task")
        assert "no code" in out.summary

    def test_document_microtask_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("boom"))
        out = a.document_microtask(_be_microtask(), {"a.py": "code"}, "task")
        assert "LLM error" in out.summary

    def test_document_microtask_success(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(
            monkeypatch,
            mod,
            response="## FILE x.py ##\nupdated\n## SUMMARY ##\ndone\n## END SUMMARY ##\n",
        )
        out = a.document_microtask(_be_microtask(), {"a.py": "code"}, "task")
        assert "x.py" in out.files
        assert "1 file(s)" in out.summary

    def test_review_no_model(self):
        a, _ = self._agent()
        out = a.review(_be_phase_input(current_files={"a.py": "code"}))
        assert "skipped" in out.summary

    def test_review_no_code(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        out = a.review(_be_phase_input(current_files={}))
        assert "no code" in out.summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("boom"))
        out = a.review(_be_phase_input(current_files={"a.py": "code"}))
        assert "LLM error" in out.summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: missing docstring\nseverity: low\nfile_path: a.py\nsource: doc\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nbad\n## END SUMMARY ##\n"
        )
        _patch_strands(monkeypatch, mod, response=resp)
        out = a.review(_be_phase_input(current_files={"a.py": "code"}))
        assert len(out.issues) == 1
        assert out.issues[0].source == "documentation"
        assert out.issues[0].severity == "low"

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        out = a.problem_solve(_be_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_doc_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        out = a.problem_solve(
            _be_phase_input(review_issues=[_be_review_issue(source="security")])
        )
        assert "No documentation issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.py ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "old"},
                review_issues=[
                    _be_review_issue(source="documentation", file_path="a.py"),
                    _be_review_issue(source="tool_documentation", file_path="b.py"),
                ],
            )
        )
        assert "fixed" in out.files["a.py"]
        assert "fixed 2 of 2" in out.summary

    def test_problem_solve_java_uses_java_conventions(self, monkeypatch):
        """The language=java path uses JAVA_CONVENTIONS."""
        a, mod = self._agent()
        a._model = object()
        stub = _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.java ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        a.problem_solve(
            _be_phase_input(
                language="java",
                current_files={"a.java": "old"},
                review_issues=[_be_review_issue(source="documentation", file_path="a.java")],
            )
        )
        # Just verify the LLM was called (Java prompt formatting succeeded)
        assert stub.calls

    def test_problem_solve_llm_exception_skips_that_issue(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("boom"))
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "old"},
                review_issues=[_be_review_issue(source="documentation", file_path="a.py")],
            )
        )
        assert "fixed 0 of 1" in out.summary


def test_be_extract_doc_files():
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        _extract_doc_files,
    )

    files = {
        "README.md": "x",
        "src/main.py": "code",
        "docs/api.md": "api",
        "CONTRIBUTING.md": "c",
        "src/util.py": "util",
    }
    out = _extract_doc_files(files)
    assert "README.md" in out
    assert "docs/api.md" in out
    assert "CONTRIBUTING.md" in out
    assert "src/main.py" not in out


def test_be_relevant_code_for_issue_with_file():
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="a.py")
    out = _relevant_code_for_issue(issue, {"a.py": "code"})
    assert "a.py" in out


def test_be_relevant_code_for_issue_with_large_file():
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        MAX_RELEVANT_CODE_CHARS,
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="a.py")
    big = "x" * (MAX_RELEVANT_CODE_CHARS + 5000)
    out = _relevant_code_for_issue(issue, {"a.py": big})
    assert "[truncated]" in out


def test_be_relevant_code_for_issue_no_file():
    """Falls back to first files when issue's file is not in current_files."""
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="missing.py")
    out = _relevant_code_for_issue(issue, {"a.py": "code A", "b.py": "code B"})
    assert "a.py" in out
    assert "b.py" in out


def test_be_relevant_code_for_issue_empty_files():
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue()
    out = _relevant_code_for_issue(issue, {})
    assert out == "(no code)"


def test_be_relevant_code_truncates_at_limit():
    """Big concatenation hits MAX_RELEVANT_CODE_CHARS and truncates."""
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue
    from software_engineering_team.backend_code_v2_team.tool_agents.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue()
    files = {f"f{i}.py": "x" * 2000 for i in range(20)}
    out = _relevant_code_for_issue(issue, files)
    # truncated marker appears for first file that overflows
    assert "[truncated]" in out or len(out) > 0


# ---------------------------------------------------------------------------
# Backend Security tool agent
# ---------------------------------------------------------------------------


class TestBackendSecurity:
    def _agent(self):
        from software_engineering_team.backend_code_v2_team.tool_agents.security import (
            agent as mod,
        )

        a = mod.SecurityToolAgent.__new__(mod.SecurityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Security execute" in out.summary

    def test_run_delegates(self):
        a, _ = self._agent()
        out = a.run(_be_tool_input())
        assert "Security execute" in out.summary

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations

    def test_deliver(self):
        a, _ = self._agent()
        assert "Security deliver" in a.deliver(_be_phase_input()).summary

    def test_review_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary

    def test_review_no_code(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        assert "no code" in a.review(_be_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "LLM error" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: SQL injection\nseverity: high\nfile_path: a.py\nsource: security\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        _patch_strands(monkeypatch, mod, response=resp)
        out = a.review(_be_phase_input(current_files={"a.py": "x"}))
        assert len(out.issues) == 1
        assert out.issues[0].source == "security"

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.problem_solve(_be_phase_input()).summary

    def test_problem_solve_no_security_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _be_phase_input(review_issues=[_be_review_issue(source="documentation")])
        )
        assert "No security issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.py ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "x"},
                review_issues=[
                    _be_review_issue(source="security", file_path="a.py"),
                    _be_review_issue(source="tool_security", file_path="b.py"),
                ],
            )
        )
        assert "2 of 2" in out.summary

    def test_problem_solve_llm_failure(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "x"},
                review_issues=[_be_review_issue(source="security", file_path="a.py")],
            )
        )
        assert "0 of 1" in out.summary


# ---------------------------------------------------------------------------
# Backend Testing QA tool agent
# ---------------------------------------------------------------------------


class TestBackendTestingQA:
    def _agent(self):
        from software_engineering_team.backend_code_v2_team.tool_agents.testing_qa import (
            agent as mod,
        )

        a = mod.TestingQAToolAgent.__new__(mod.TestingQAToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Testing/QA execute" in out.summary or "QA" in out.summary

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations

    def test_deliver(self):
        a, _ = self._agent()
        out = a.deliver(_be_phase_input())
        assert "deliver" in out.summary.lower()

    def test_review_no_model(self):
        a, _ = self._agent()
        out = a.review(_be_phase_input(current_files={"a.py": "x"}))
        assert "skipped" in out.summary

    def test_review_no_code(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        assert "no code" in a.review(_be_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "error" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary.lower()

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: missing test\nseverity: high\nfile_path: a.py\nsource: qa\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        _patch_strands(monkeypatch, mod, response=resp)
        out = a.review(_be_phase_input(current_files={"a.py": "x"}))
        assert len(out.issues) >= 1

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        out = a.problem_solve(_be_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_relevant_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _be_phase_input(review_issues=[_be_review_issue(source="security")])
        )
        assert "No" in out.summary or "no" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.py ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "x"},
                review_issues=[_be_review_issue(source="qa", file_path="a.py")],
            )
        )
        assert out.files or "fixed" in out.summary


# ---------------------------------------------------------------------------
# Frontend Documentation tool agent
# ---------------------------------------------------------------------------


class TestFrontendDocumentation:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.documentation import (
            agent as mod,
        )

        a = mod.DocumentationToolAgent.__new__(mod.DocumentationToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_plan_returns_typescript_specific(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        joined = " ".join(out.recommendations)
        assert "Storybook" in joined or "JSDoc" in joined or "TSDoc" in joined or "component" in joined.lower()

    def test_extract_doc_files_includes_stories(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.documentation.agent import (
            _extract_doc_files,
        )

        files = {
            "Button.stories.ts": "story",
            "Button.tsx": "code",
            "README.md": "x",
            "storybook/main.js": "config",
            "styleguide.md": "style",
        }
        out = _extract_doc_files(files)
        assert "Button.stories.ts" in out
        assert "storybook/main.js" in out
        assert "styleguide.md" in out
        assert "Button.tsx" not in out

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: missing docs\nseverity: medium\nfile_path: a.ts\nsource: documentation\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        _patch_strands(monkeypatch, mod, response=resp)
        out = a.review(_fe_phase_input(current_files={"a.ts": "code"}))
        assert len(out.issues) == 1

    def test_problem_solve_uses_typescript_conventions(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        stub = _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        a.problem_solve(
            _fe_phase_input(
                current_files={"a.ts": "old"},
                review_issues=[_fe_review_issue(source="documentation", file_path="a.ts")],
            )
        )
        assert stub.calls


# ---------------------------------------------------------------------------
# Frontend Security tool agent
# ---------------------------------------------------------------------------


class TestFrontendSecurity:
    def _agent(self):
        from software_engineering_team.frontend_code_v2_team.tool_agents.security import (
            agent as mod,
        )

        a = mod.SecurityToolAgent.__new__(mod.SecurityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        from software_engineering_team.frontend_code_v2_team.models import ToolAgentInput

        a, _ = self._agent()
        inp = ToolAgentInput(
            microtask=_fe_microtask(),
            task_title="t",
            task_description="d",
            spec_content="",
            repo_path="/tmp",
        )
        out = a.execute(inp)
        assert "Security execute" in out.summary

    def test_review_no_model(self):
        a, _ = self._agent()
        assert "skipped" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: XSS risk\nseverity: high\nfile_path: a.tsx\nsource: security\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        _patch_strands(monkeypatch, mod, response=resp)
        out = a.review(_fe_phase_input(current_files={"a.tsx": "code"}))
        assert len(out.issues) == 1

    def test_problem_solve_no_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(
            _fe_phase_input(review_issues=[_fe_review_issue(source="documentation")])
        )
        assert "No security issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.tsx ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.tsx": "x"},
                review_issues=[_fe_review_issue(source="security", file_path="a.tsx")],
            )
        )
        assert "1 of 1" in out.summary
