"""Unit tests for the shared code-v2 tool-agent base and helpers."""

from __future__ import annotations

import logging

import pytest
from code_review_agent import CodeReviewUnavailableError
from code_review_agent.profiles import ReviewProfile

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.tool_agent_base import (
    DEFAULT_MAX_RELEVANT_CODE_CHARS,
    BaseReviewToolAgent,
    lenient_json_object,
    relevant_code_for_issue,
)
from software_engineering_team.shared.v2_models import ReviewIssue

# ---------------------------------------------------------------------------
# relevant_code_for_issue
# ---------------------------------------------------------------------------


def test_relevant_code_prefers_issue_file():
    issue = ReviewIssue(file_path="a.ts")
    out = relevant_code_for_issue(issue, {"a.ts": "code", "b.ts": "other"})
    assert out == "--- a.ts ---\ncode"


def test_relevant_code_truncates_large_issue_file():
    issue = ReviewIssue(file_path="a.ts")
    big = "x" * (DEFAULT_MAX_RELEVANT_CODE_CHARS + 100)
    out = relevant_code_for_issue(issue, {"a.ts": big})
    assert "[truncated]" in out
    assert len(out) < len(big)


def test_relevant_code_falls_back_to_first_files():
    issue = ReviewIssue(file_path="missing.ts")
    out = relevant_code_for_issue(issue, {"a.ts": "A", "b.ts": "B"})
    assert "a.ts" in out and "b.ts" in out


def test_relevant_code_multifile_truncation():
    issue = ReviewIssue(file_path="")
    files = {f"f{i}.ts": "y" * 3000 for i in range(10)}
    out = relevant_code_for_issue(issue, files, max_chars=5000)
    assert "[truncated]" in out


def test_relevant_code_empty_returns_placeholder():
    assert relevant_code_for_issue(ReviewIssue(), {}) == "(no code)"


# ---------------------------------------------------------------------------
# lenient_json_object
# ---------------------------------------------------------------------------


def test_lenient_json_direct():
    data = lenient_json_object(
        '{"a": 1}', logger=logging.getLogger("t"), context="ctx", on_fail_msg="x"
    )
    assert data == {"a": 1}


def test_lenient_json_extracts_object_from_prose():
    data = lenient_json_object(
        'prefix {"a": 2} suffix', logger=logging.getLogger("t"), context="ctx", on_fail_msg="x"
    )
    assert data == {"a": 2}


def test_lenient_json_no_object_returns_empty(caplog):
    with caplog.at_level(logging.WARNING):
        data = lenient_json_object(
            "no json here", logger=logging.getLogger("t"), context="Review", on_fail_msg="zero."
        )
    assert data == {}
    assert "contained no JSON object" in caplog.text


def test_lenient_json_malformed_inner_returns_empty(caplog):
    with caplog.at_level(logging.WARNING):
        data = lenient_json_object(
            "junk {bad: } more",
            logger=logging.getLogger("t"),
            context="Review",
            on_fail_msg="zero.",
        )
    assert data == {}
    assert "did not parse as JSON" in caplog.text


# ---------------------------------------------------------------------------
# BaseReviewToolAgent template behavior (via a minimal subclass)
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, response):
        self._response = response

    def __call__(self, prompt):
        return self._response


def _stub_review_parser(raw):
    return {
        "issues": [
            {"severity": "high", "description": raw, "file_path": "x", "recommendation": "r"}
        ]
    }


def _stub_single_issue_parser(raw):
    return {"files": {"x.ts": "fixed"}} if raw else {"files": {}}


class _DemoAgent(BaseReviewToolAgent):
    name = "Demo"
    empty_label = "demo issues"
    issue_source = "demo"
    problem_solve_sources = ("demo",)
    review_prompt = "task={task_description} code={code}"
    problem_solving_prompt = "src={source} sev={severity} desc={description} fp={file_path} rec={recommendation} code={current_code}"
    max_code_chars = 1000
    review_parse_mode = "text"
    default_recommendation = "Fix demo."
    plan_recommendations = ["do a demo thing"]
    plan_summary = "Demo planning."
    _parse_review = staticmethod(_stub_review_parser)
    _parse_single_issue = staticmethod(_stub_single_issue_parser)


class _Microtask:
    id = "mt-1"


class _Input:
    def __init__(self, current_files=None, review_issues=None, task_description="d"):
        self.current_files = current_files or {}
        self.review_issues = review_issues or []
        self.task_description = task_description
        self.microtask = _Microtask()


# Provide a module-level Agent symbol so _agent_factory (which resolves Agent
# from the subclass's defining module) can find and patch it.
Agent = None


def _patch_agent(monkeypatch, factory):
    """Patch ``Agent`` on this test module — the demo subclass's home module."""
    import sys

    monkeypatch.setattr(sys.modules[_DemoAgent.__module__], "Agent", factory, raising=False)


def _make(monkeypatch, response):
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = object()
    agent.llm = None
    _patch_agent(monkeypatch, lambda *a, **k: _FakeAgent(response))
    return agent


def test_run_delegates_to_execute():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = None
    agent.llm = None
    out = agent.run(_Input())
    assert "Demo execute" in out.summary


def test_execute_logs_and_returns_stub(caplog):
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = None
    agent.llm = None
    with caplog.at_level(logging.INFO):
        out = agent.execute(_Input())
    assert out.summary == "Demo execute — no changes applied."


def test_plan_returns_static():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = None
    out = agent.plan(_Input())
    assert out.recommendations == ["do a demo thing"]
    assert out.summary == "Demo planning."


def test_deliver():
    agent = _DemoAgent.__new__(_DemoAgent)
    assert agent.deliver(_Input()).summary == "Demo deliver."


def test_review_no_model():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = None
    assert "skipped (no LLM)" in agent.review(_Input(current_files={"a": "b"})).summary


def test_review_no_code():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = object()
    assert "no code" in agent.review(_Input(current_files={})).summary


def test_review_finds_issues(monkeypatch):
    agent = _make(monkeypatch, "raw-review")
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert len(out.issues) == 1
    assert out.issues[0].source == "demo"
    assert "Demo review: 1 issue(s) found." == out.summary


def test_review_llm_exception(monkeypatch):
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = object()
    agent.llm = None

    def boom(*a, **k):
        raise RuntimeError("err")

    _patch_agent(monkeypatch, boom)
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert "failed (LLM error)" in out.summary


# ---------------------------------------------------------------------------
# review_via_engine: opt-in routing through the shared code-review engine
# ---------------------------------------------------------------------------


class _EngineStubClient(DummyLLMClient):
    """Returns one canned engine-shaped response for every chunk-review call."""

    def __init__(self, response):
        super().__init__()
        self._response = response

    def complete_json(self, prompt, **kwargs):
        return self._response


class _EngineDemoAgent(_DemoAgent):
    """Demo reviewer that opts into the shared engine with a profile."""

    review_via_engine = True
    review_profile = ReviewProfile.SPEC_CONFORMANCE


def _engine_agent(response):
    agent = _EngineDemoAgent.__new__(_EngineDemoAgent)
    agent._model = object()
    agent.llm = _EngineStubClient(response)
    return agent


def test_engine_review_maps_issues_and_source():
    agent = _engine_agent(
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "spec-compliance",
                    "file_path": "a.ts",
                    "description": "missing pagination",
                    "suggestion": "add page params",
                }
            ],
            "summary": "needs work",
        }
    )
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert len(out.issues) == 1
    issue = out.issues[0]
    assert issue.source == "demo"
    assert issue.severity == "high"
    # CodeReviewIssue.suggestion is mapped onto ReviewIssue.recommendation.
    assert issue.recommendation == "add page params"
    assert issue.file_path == "a.ts"
    assert "Demo review: 1 issue(s) found." == out.summary


def test_engine_review_clean_pass_reports_no_issues():
    agent = _engine_agent({"approved": True, "issues": [], "summary": "ok"})
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert out.issues == []
    assert "Demo review: 0 issue(s) found." == out.summary


def test_engine_review_skips_without_code():
    agent = _engine_agent({"approved": True, "issues": [], "summary": "ok"})
    out = agent.review(_Input(current_files={}))
    assert "skipped (no code)" in out.summary


class _RaisingEngine:
    """Stand-in for ``CodeReviewAgent`` whose ``run`` raises a given exception."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, _llm):
        return self

    def run(self, _input):
        raise self._exc


def test_engine_review_degrades_on_unavailable(monkeypatch):
    monkeypatch.setattr(
        "code_review_agent.CodeReviewAgent",
        _RaisingEngine(CodeReviewUnavailableError("engine down")),
    )
    agent = _EngineDemoAgent.__new__(_EngineDemoAgent)
    agent._model = object()
    agent.llm = None
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert "failed (LLM error)" in out.summary


def test_engine_review_propagates_unexpected_error(monkeypatch):
    monkeypatch.setattr("code_review_agent.CodeReviewAgent", _RaisingEngine(TypeError("boom")))
    agent = _EngineDemoAgent.__new__(_EngineDemoAgent)
    agent._model = object()
    agent.llm = None
    with pytest.raises(TypeError):
        agent.review(_Input(current_files={"a.ts": "code"}))


def test_engine_review_problem_solve_unchanged(monkeypatch):
    # Issues produced via the engine still flow through the unchanged
    # one-at-a-time problem_solve path keyed on ``source``.
    agent = _EngineDemoAgent.__new__(_EngineDemoAgent)
    agent._model = object()
    agent.llm = None
    _patch_agent(monkeypatch, lambda *a, **k: _FakeAgent("raw"))
    issue = ReviewIssue(source="demo", description="d", file_path="x.ts", recommendation="r")
    out = agent.problem_solve(_Input(current_files={"x.ts": "old"}, review_issues=[issue]))
    assert "fixed 1 of 1" in out.summary


def test_problem_solve_no_model():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = None
    assert "problem_solve skipped" in agent.problem_solve(_Input()).summary


def test_problem_solve_no_matching_issues():
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = object()
    out = agent.problem_solve(_Input(review_issues=[ReviewIssue(source="other")]))
    assert out.summary == "No demo issues to fix."


def test_problem_solve_fixes(monkeypatch):
    agent = _make(monkeypatch, "## FILE x.ts ##\nfixed")
    out = agent.problem_solve(
        _Input(
            current_files={"x.ts": "old"},
            review_issues=[ReviewIssue(source="demo", file_path="x.ts")],
        )
    )
    assert "fixed 1 of 1 issue(s) (one at a time)." in out.summary


def test_problem_solve_llm_exception(monkeypatch):
    agent = _DemoAgent.__new__(_DemoAgent)
    agent._model = object()
    agent.llm = None

    def boom(*a, **k):
        raise RuntimeError("err")

    _patch_agent(monkeypatch, boom)
    out = agent.problem_solve(
        _Input(
            current_files={"x.ts": "old"},
            review_issues=[ReviewIssue(source="demo", file_path="x.ts")],
        )
    )
    assert "fixed 0 of 1" in out.summary


def test_constructor_resolves_text_model(monkeypatch):
    seen = []

    def _record(llm, *, response_format="json"):
        seen.append(response_format)
        return object()

    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_strands_model", _record
    )
    agent = _DemoAgent(llm=None)
    assert agent._model is not None
    assert seen == ["text"]  # uses_json_model defaults False


class _JsonDemoAgent(_DemoAgent):
    uses_json_model = True


def test_constructor_resolves_json_model_when_enabled(monkeypatch):
    seen = []

    def _record(llm, *, response_format="json"):
        seen.append(response_format)
        return object()

    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_strands_model", _record
    )
    agent = _JsonDemoAgent(llm=None)
    assert agent._model is not None and agent._model_json is not None
    assert "text" in seen and "json" in seen


@pytest.mark.parametrize("mode", ["json"])
def test_review_json_mode(monkeypatch, mode):
    class _JsonReview(_DemoAgent):
        review_parse_mode = "json"

    agent = _JsonReview.__new__(_JsonReview)
    agent._model = object()
    agent.llm = None
    _patch_agent(
        monkeypatch,
        lambda *a, **k: _FakeAgent('{"issues": [{"description": "d"}], "summary": "s"}'),
    )
    out = agent.review(_Input(current_files={"a.ts": "code"}))
    assert len(out.issues) == 1
    assert out.issues[0].source == "demo"
