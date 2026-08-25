"""More tests for the backend_code_v2 and frontend_code_v2 tool agents.

Covers documentation, security, testing_qa and the helper functions
``_relevant_code_for_issue`` and ``_extract_doc_files``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Backend helpers / models
# ---------------------------------------------------------------------------


def _be_microtask():
    """Build a minimal backend Microtask fixture for tool-agent tests."""
    from software_engineering_team.codegen_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _be_phase_input(**kwargs):
    """Build a backend ToolAgentPhaseInput fixture, overriding fields via kwargs."""
    from software_engineering_team.codegen_team.models import (
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
    """Build a backend ReviewIssue fixture, overriding fields via kwargs."""
    from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(source="documentation", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


def _be_tool_input():
    """Build a minimal backend ToolAgentInput fixture for execute()/run() tests."""
    from software_engineering_team.codegen_team.models import ToolAgentInput

    return ToolAgentInput(microtask=_be_microtask(), repo_path="/tmp", existing_code="", language="python")


# ---------------------------------------------------------------------------
# Frontend helpers / models
# ---------------------------------------------------------------------------


def _fe_microtask():
    """Build a minimal frontend Microtask fixture for tool-agent tests."""
    from software_engineering_team.codegen_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _fe_phase_input(**kwargs):
    """Build a frontend ToolAgentPhaseInput fixture, overriding fields via kwargs."""
    from software_engineering_team.codegen_team.models import (
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
    """Build a frontend ReviewIssue fixture, overriding fields via kwargs."""
    from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(source="documentation", severity="medium", description="d", file_path="", recommendation="")
    base.update(kwargs)
    return ReviewIssue(**base)


# ---------------------------------------------------------------------------
# Backend Documentation tool agent
# ---------------------------------------------------------------------------


class _StubStrandsAgent:
    """Callable that mimics a strands Agent.__call__ returning canned text."""

    def __init__(self, response="## SUMMARY ##\nok\n## END SUMMARY ##\n", raise_exc=None):
        """Configure the canned response (or exception) this stub will produce."""
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, prompt):
        """Record the prompt and return the canned response, or raise raise_exc."""
        self.calls.append(prompt)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _patch_strands(monkeypatch, mod, response="", raise_exc=None):
    """Patch ``mod.Agent`` with a `_StubStrandsAgent` and return the stub."""
    stub = _StubStrandsAgent(response=response, raise_exc=raise_exc)
    monkeypatch.setattr(mod, "Agent", lambda *a, **kw: stub)
    return stub


class TestBackendDocumentation:
    """Backend DocumentationToolAgent: document_microtask, review, and problem_solve."""

    def _agent(self):
        """Build a bare DocumentationToolAgent instance with no LLM configured."""
        from software_engineering_team.codegen_team.tool_agents.backend.documentation import (
            agent as mod,
        )

        a = mod.DocumentationToolAgent.__new__(mod.DocumentationToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        """execute() returns the Documentation execute stub summary."""
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Documentation execute" in out.summary

    def test_run_delegates_to_execute(self):
        """run() delegates to execute()."""
        a, _ = self._agent()
        out = a.run(_be_tool_input())
        assert "Documentation execute" in out.summary

    def test_plan_returns_recommendations(self):
        """plan() returns the exact backend documentation recommendations."""
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations == [
            "Include README updates for new features.",
            "Document API changes and new endpoints.",
            "Add docstrings for all public functions, classes, and methods.",
            "Update CONTRIBUTORS.md if applicable.",
        ]
        assert "Documentation planning" in out.summary

    def test_deliver_returns_stub(self):
        """deliver() returns the Documentation deliver stub summary."""
        a, _ = self._agent()
        out = a.deliver(_be_phase_input())
        assert "Documentation deliver" in out.summary

    def test_document_microtask_no_model(self):
        """document_microtask() skips when no LLM is configured."""
        a, _ = self._agent()
        out = a.document_microtask(_be_microtask(), {"a.py": "code"}, "task")
        assert "no LLM" in out.summary

    def test_document_microtask_no_code(self, monkeypatch):
        """document_microtask() skips when there is no code to document."""
        a, mod = self._agent()
        a._model = object()  # not None
        _patch_strands(monkeypatch, mod, response="## FILE x.py ##\nupdated\n## SUMMARY ##\nok\n## END SUMMARY ##\n")
        out = a.document_microtask(_be_microtask(), {}, "task")
        assert "no code" in out.summary

    def test_document_microtask_llm_exception(self, monkeypatch):
        """document_microtask() reports LLM error when the strands call raises."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("boom"))
        out = a.document_microtask(_be_microtask(), {"a.py": "code"}, "task")
        assert "LLM error" in out.summary

    def test_document_microtask_success(self, monkeypatch):
        """document_microtask() returns parsed files on a successful LLM response."""
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
        """review() skips when no LLM is configured."""
        a, _ = self._agent()
        out = a.review(_be_phase_input(current_files={"a.py": "code"}))
        assert "skipped" in out.summary

    def test_review_no_code(self, monkeypatch):
        """review() skips when current_files is empty."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        out = a.review(_be_phase_input(current_files={}))
        assert "no code" in out.summary

    def test_review_llm_exception(self, monkeypatch):
        """review() reports LLM error when the strands call raises."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("boom"))
        out = a.review(_be_phase_input(current_files={"a.py": "code"}))
        assert "LLM error" in out.summary

    def test_review_finds_issues(self, monkeypatch):
        """review() parses exactly one documentation issue from the LLM response."""
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
        """problem_solve() skips when no LLM is configured."""
        a, _ = self._agent()
        out = a.problem_solve(_be_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_doc_issues(self, monkeypatch):
        """problem_solve() no-ops when review_issues have no documentation sources."""
        a, mod = self._agent()
        a._model = object()
        out = a.problem_solve(
            _be_phase_input(review_issues=[_be_review_issue(source="security")])
        )
        assert "No documentation issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        """problem_solve() merges LLM file fixes for matching documentation issues."""
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
        """The language=java path injects JAVA_CONVENTIONS into the problem_solve prompt."""
        a, mod = self._agent()
        a._model = object()
        stub = _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.java ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _be_phase_input(
                language="java",
                current_files={"a.java": "old"},
                review_issues=[_be_review_issue(source="documentation", file_path="a.java")],
            )
        )
        assert len(stub.calls) == 1
        assert "Java conventions" in stub.calls[0]
        assert out.files["a.java"] == "fixed"
        assert "fixed 1 of 1" in out.summary

    def test_problem_solve_llm_exception_skips_that_issue(self, monkeypatch):
        """problem_solve() counts an LLM failure as unfixed for that issue."""
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
    """_extract_doc_files keeps README/docs/CONTRIBUTING files and excludes source files."""
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
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
    """Returns the code for the issue's own file when it exists in current_files."""
    from software_engineering_team.codegen_team.models import ReviewIssue
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="a.py")
    out = _relevant_code_for_issue(issue, {"a.py": "code"})
    assert "a.py" in out


def test_be_relevant_code_for_issue_with_large_file():
    """Truncates a single oversized file's code to MAX_RELEVANT_CODE_CHARS."""
    from software_engineering_team.codegen_team.models import ReviewIssue
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
        MAX_RELEVANT_CODE_CHARS,
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="a.py")
    big = "x" * (MAX_RELEVANT_CODE_CHARS + 5000)
    out = _relevant_code_for_issue(issue, {"a.py": big})
    assert len(out) == MAX_RELEVANT_CODE_CHARS
    assert "[truncated;" in out
    assert big not in out


def test_be_relevant_code_for_issue_no_file():
    """Falls back to first files when issue's file is not in current_files."""
    from software_engineering_team.codegen_team.models import ReviewIssue
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue(file_path="missing.py")
    out = _relevant_code_for_issue(issue, {"a.py": "code A", "b.py": "code B"})
    assert "a.py" in out
    assert "b.py" in out


def test_be_relevant_code_for_issue_empty_files():
    """Returns the "(no code)" placeholder when current_files is empty."""
    from software_engineering_team.codegen_team.models import ReviewIssue
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue()
    out = _relevant_code_for_issue(issue, {})
    assert out == "(no code)"


def test_be_relevant_code_bounds_multifile_context():
    """Truncates the combined multi-file context once it exceeds the char bound."""
    from software_engineering_team.codegen_team.models import ReviewIssue
    from software_engineering_team.codegen_team.tool_agents.backend.documentation.agent import (
        _relevant_code_for_issue,
    )

    issue = ReviewIssue()
    files = {f"f{i}.py": "x" * 2000 for i in range(20)}
    out = _relevant_code_for_issue(issue, files)
    assert "[truncated;" in out
    assert "f0.py" in out
    assert "f19.py" not in out


# ---------------------------------------------------------------------------
# Backend Security tool agent
# ---------------------------------------------------------------------------


class TestBackendSecurity:
    """Backend SecurityToolAgent: review-only; no problem_solve self-fix."""

    def _agent(self):
        """Build a bare SecurityToolAgent instance with no LLM configured."""
        from software_engineering_team.codegen_team.tool_agents.backend.security import (
            agent as mod,
        )

        a = mod.SecurityToolAgent.__new__(mod.SecurityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        """execute() returns the Security execute stub summary."""
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Security execute" in out.summary

    def test_run_delegates(self):
        """run() delegates to execute()."""
        a, _ = self._agent()
        out = a.run(_be_tool_input())
        assert "Security execute" in out.summary

    def test_plan(self):
        """plan() returns the exact backend security recommendation."""
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations == [
            "Consider injection prevention, auth checks, and secure defaults."
        ]

    def test_deliver(self):
        """deliver() returns the Security deliver stub summary."""
        a, _ = self._agent()
        assert "Security deliver" in a.deliver(_be_phase_input()).summary

    def test_review_no_model(self):
        """review() skips when no LLM is configured."""
        a, _ = self._agent()
        assert "skipped" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary

    def test_review_no_code(self, monkeypatch):
        """review() skips when current_files is empty."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        assert "no code" in a.review(_be_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        """review() reports LLM error when the strands call raises."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "LLM error" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        """review() parses exactly one security issue from the LLM response."""
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

    def test_review_cache_hit_skips_second_llm_call(self, monkeypatch):
        """Two review() calls with identical current_files/task_description
        hit the shared tool-agent cache on the second call: the LLM is
        invoked only once through the full SecurityToolAgent -> Backend
        ReviewToolAgent -> BaseReviewToolAgent -> LlmToolAgentBase chain."""
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: SQL injection\nseverity: high\nfile_path: a.py\nsource: security\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        stub = _patch_strands(monkeypatch, mod, response=resp)

        first = a.review(_be_phase_input(current_files={"a.py": "x"}))
        second = a.review(_be_phase_input(current_files={"a.py": "x"}))

        assert len(stub.calls) == 1
        assert first.summary == second.summary
        assert [i.description for i in first.issues] == [i.description for i in second.issues]

    def test_no_problem_solve_capability(self):
        """Security is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


# ---------------------------------------------------------------------------
# Backend Testing QA tool agent
# ---------------------------------------------------------------------------


class TestBackendTestingQA:
    """Backend TestingQAToolAgent: review-only; no problem_solve self-fix."""

    def _agent(self):
        """Build a bare TestingQAToolAgent instance with no LLM configured."""
        from software_engineering_team.codegen_team.tool_agents.backend.testing_qa import (
            agent as mod,
        )

        a = mod.TestingQAToolAgent.__new__(mod.TestingQAToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        """execute() returns the Testing/QA execute stub summary."""
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Testing/QA execute" in out.summary

    def test_plan(self):
        """plan() returns the exact Testing/QA recommendation."""
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations == ["Include unit and integration tests in the plan."]

    def test_deliver(self):
        """deliver() returns the Testing/QA deliver stub summary."""
        a, _ = self._agent()
        out = a.deliver(_be_phase_input())
        assert "Testing/QA deliver" in out.summary

    def test_review_no_model(self):
        """review() skips when no LLM is configured."""
        a, _ = self._agent()
        out = a.review(_be_phase_input(current_files={"a.py": "x"}))
        assert "skipped" in out.summary

    def test_review_no_code(self, monkeypatch):
        """review() skips when current_files is empty."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod)
        assert "no code" in a.review(_be_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        """review() reports LLM error when the strands call raises."""
        a, mod = self._agent()
        a._model = object()
        _patch_strands(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "LLM error" in a.review(_be_phase_input(current_files={"a.py": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        """review() parses exactly one QA issue with the expected fields."""
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
        assert len(out.issues) == 1
        assert out.issues[0].source == "qa"
        assert out.issues[0].severity == "high"
        assert out.issues[0].description == "missing test"
        assert out.issues[0].file_path == "a.py"

    def test_no_problem_solve_capability(self):
        """QA is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


# ---------------------------------------------------------------------------
# Frontend Documentation tool agent
# ---------------------------------------------------------------------------


class TestFrontendDocumentation:
    """Frontend DocumentationToolAgent: TypeScript-specific plan, review, and problem_solve."""

    def _agent(self):
        """Build a bare DocumentationToolAgent instance with no LLM configured."""
        from software_engineering_team.codegen_team.tool_agents.frontend.documentation import (
            agent as mod,
        )

        a = mod.DocumentationToolAgent.__new__(mod.DocumentationToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_plan_returns_typescript_specific(self):
        """plan() returns the exact frontend documentation recommendations."""
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert out.recommendations == [
            "Include README updates for new features and components.",
            "Document component props and usage examples.",
            "Add JSDoc/TSDoc comments for all public functions and components.",
            "Update Storybook stories for new UI components.",
            "Update CONTRIBUTORS.md if applicable.",
        ]

    def test_extract_doc_files_includes_stories(self):
        """_extract_doc_files includes Storybook/stories and excludes component source."""
        from software_engineering_team.codegen_team.tool_agents.frontend.documentation.agent import (
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
        """review() parses exactly one documentation issue from the LLM response."""
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
        assert out.issues[0].source == "documentation"
        assert out.issues[0].severity == "medium"
        assert out.issues[0].description == "missing docs"

    def test_problem_solve_uses_typescript_conventions(self, monkeypatch):
        """problem_solve() injects TypeScript conventions and applies the LLM file fix."""
        a, mod = self._agent()
        a._model = object()
        stub = _patch_strands(
            monkeypatch,
            mod,
            response="## FILE a.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n",
        )
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.ts": "old"},
                review_issues=[_fe_review_issue(source="documentation", file_path="a.ts")],
            )
        )
        assert len(stub.calls) == 1
        assert "TypeScript conventions" in stub.calls[0]
        assert out.files.get("a.ts") == "fixed"


# ---------------------------------------------------------------------------
# Frontend Security tool agent
# ---------------------------------------------------------------------------


class TestFrontendSecurity:
    """Frontend SecurityToolAgent: review-only; no problem_solve self-fix."""

    def _agent(self):
        """Build a bare SecurityToolAgent instance with no LLM configured."""
        from software_engineering_team.codegen_team.tool_agents.frontend.security import (
            agent as mod,
        )

        a = mod.SecurityToolAgent.__new__(mod.SecurityToolAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute_returns_stub(self):
        """execute() returns the Security execute stub summary."""
        from software_engineering_team.codegen_team.models import ToolAgentInput

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
        """review() skips when no LLM is configured."""
        a, _ = self._agent()
        assert "skipped" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        """review() parses exactly one security issue from the LLM response."""
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
        assert out.issues[0].source == "security"
        assert out.issues[0].severity == "high"
        assert out.issues[0].description == "XSS risk"

    def test_review_cache_hit_skips_second_llm_call(self, monkeypatch):
        """Two review() calls with identical current_files/task_description
        hit the shared tool-agent cache on the second call: the LLM is
        invoked only once through the full SecurityToolAgent -> BaseReview
        ToolAgent -> LlmToolAgentBase chain."""
        a, mod = self._agent()
        a._model = object()
        resp = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: XSS risk\nseverity: high\nfile_path: a.tsx\nsource: security\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix\n## END SUMMARY ##\n"
        )
        stub = _patch_strands(monkeypatch, mod, response=resp)

        first = a.review(_fe_phase_input(current_files={"a.tsx": "code"}))
        second = a.review(_fe_phase_input(current_files={"a.tsx": "code"}))

        assert len(stub.calls) == 1
        assert first.summary == second.summary
        assert [i.description for i in first.issues] == [i.description for i in second.issues]

    def test_no_problem_solve_capability(self):
        """Security is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")
