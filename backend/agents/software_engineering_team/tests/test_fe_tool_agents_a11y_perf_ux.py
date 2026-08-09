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

    base = dict(
        source="accessibility", severity="medium", description="d", file_path="", recommendation=""
    )
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
        a._model_json = None
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
        a._model_json = object()
        _patch(monkeypatch, mod, "{}")
        assert "no code" in a.review(_fe_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        assert "LLM error" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
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
        a._model_json = object()
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
        a._model_json = object()
        _patch(monkeypatch, mod, "not json at all")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []

    def test_review_malformed_inner_json(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, "garbage {malformed: } more")
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert out.issues == []

    def test_no_problem_solve_capability(self):
        """Accessibility is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


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
        a._model_json = None
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
        a._model_json = object()
        assert "no code" in a.review(_fe_phase_input(current_files={})).summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("e"))
        assert "LLM error" in a.review(_fe_phase_input(current_files={"a.ts": "x"})).summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
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
        a._model_json = object()
        _patch(monkeypatch, mod, "not json")
        assert a.review(_fe_phase_input(current_files={"a.ts": "x"})).issues == []

    def test_no_problem_solve_capability(self):
        """Performance is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


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
        a._model_json = None
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
        a._model_json = object()
        out = a.review(_fe_phase_input(current_files={}))
        assert "no code" in out.summary

    def test_review_llm_exception(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
        _patch(monkeypatch, mod, raise_exc=RuntimeError("err"))
        out = a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
        assert "LLM error" in out.summary

    def test_review_finds_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        a._model_json = object()
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

    def test_no_problem_solve_capability(self):
        """UX/Usability is review-only: fixing its findings is the coding agent's job."""
        a, _ = self._agent()
        assert not hasattr(a, "problem_solve")
        assert not hasattr(a, "problem_solve_sources")


# ---------------------------------------------------------------------------
# Dual-model routing: review/plan must use the JSON-mode model. The reviewer
# flagged a silent zero-issues failure mode when review ran in text mode and
# the LLM returned prose — JSON mode closes it.
# ---------------------------------------------------------------------------


class _ModelRecordingAgent:
    """Callable agent stub that always returns the fixed ``response`` given at construction."""

    def __init__(self, response):
        self._response = response

    def __call__(self, prompt):
        return self._response


def _patch_recording(monkeypatch, mod, response):
    """Patch ``mod.Agent`` and record the ``model`` kwarg passed to each construction."""
    calls: list = []

    def fake_agent(*args, **kwargs):
        calls.append(kwargs.get("model"))
        return _ModelRecordingAgent(response)

    monkeypatch.setattr(mod, "Agent", fake_agent)
    return calls


def test_accessibility_review_uses_json_mode_model(monkeypatch) -> None:
    """The review path must route through ``_model_json`` so a prose
    response from the LLM is biased toward returning JSON; routing it
    through ``_model`` (text mode) would silently drop WCAG findings."""
    from software_engineering_team.frontend_code_v2_team.tool_agents.accessibility import (
        agent as mod,
    )

    a = mod.AccessibilityToolAgent.__new__(mod.AccessibilityToolAgent)
    text_sentinel = object()
    json_sentinel = object()
    a._model = text_sentinel
    a._model_json = json_sentinel
    a.llm = None
    calls = _patch_recording(monkeypatch, mod, json.dumps({"issues": [], "summary": ""}))

    a.review(_fe_phase_input(current_files={"a.tsx": "<img/>"}))

    assert calls and all(c is json_sentinel for c in calls), (
        "review() must invoke Agent(model=_model_json); "
        f"got {[id(c) for c in calls]} vs json={id(json_sentinel)} text={id(text_sentinel)}"
    )


def test_performance_review_uses_json_mode_model(monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.performance import (
        agent as mod,
    )

    a = mod.PerformanceToolAgent.__new__(mod.PerformanceToolAgent)
    text_sentinel = object()
    json_sentinel = object()
    a._model = text_sentinel
    a._model_json = json_sentinel
    a.llm = None
    calls = _patch_recording(monkeypatch, mod, json.dumps({"issues": [], "summary": ""}))

    a.review(_fe_phase_input(current_files={"a.ts": "x"}))

    assert calls and all(c is json_sentinel for c in calls)


def test_ux_usability_plan_and_review_use_json_mode_model(monkeypatch) -> None:
    """Both plan() and review() on UX usability ask for strict JSON."""
    from software_engineering_team.frontend_code_v2_team.tool_agents.ux_usability import (
        agent as mod,
    )

    a = mod.UxUsabilityToolAgent.__new__(mod.UxUsabilityToolAgent)
    text_sentinel = object()
    json_sentinel = object()
    a._model = text_sentinel
    a._model_json = json_sentinel
    a.llm = None

    calls = _patch_recording(monkeypatch, mod, json.dumps({"summary": "ok"}))
    a.plan(_fe_phase_input(task_description="some task"))
    assert calls and all(c is json_sentinel for c in calls), "plan() must use _model_json"

    calls = _patch_recording(monkeypatch, mod, json.dumps({"issues": [], "summary": "ok"}))
    a.review(_fe_phase_input(current_files={"a.tsx": "x"}))
    assert calls and all(c is json_sentinel for c in calls), "review() must use _model_json"


def test_a11y_perf_ux_constructor_resolves_both_models_with_distinct_modes(monkeypatch) -> None:
    """All three tool agents must call ``resolve_strands_model`` twice —
    once for the text-mode ``_model`` and once for the json-mode
    ``_model_json``. Verified by recording every ``response_format``
    requested at the resolver boundary so we lock in the JSON path
    cannot silently fall back to text mode for the review/plan calls."""
    from software_engineering_team.frontend_code_v2_team.tool_agents.accessibility import (
        agent as a11y_mod,
    )
    from software_engineering_team.frontend_code_v2_team.tool_agents.performance import (
        agent as perf_mod,
    )
    from software_engineering_team.frontend_code_v2_team.tool_agents.ux_usability import (
        agent as ux_mod,
    )

    for cls in (
        a11y_mod.AccessibilityToolAgent,
        perf_mod.PerformanceToolAgent,
        ux_mod.UxUsabilityToolAgent,
    ):
        formats_seen: list[str] = []

        def _record(llm, *, response_format="json", _store=formats_seen):  # noqa: ANN001
            _store.append(response_format)
            return object()

        monkeypatch.setattr(
            "llm_service.strands_model.resolve_strands_model",
            _record,
        )

        instance = cls(llm=None)

        assert instance._model is not None, f"{cls.__name__}: text-mode _model missing"
        assert instance._model_json is not None, f"{cls.__name__}: json-mode _model_json missing"
        assert "text" in formats_seen, (
            f"{cls.__name__}: constructor must resolve a text-mode model "
            f"(formats seen: {formats_seen})"
        )
        assert "json" in formats_seen, (
            f"{cls.__name__}: constructor must resolve a json-mode model "
            f"(formats seen: {formats_seen})"
        )
