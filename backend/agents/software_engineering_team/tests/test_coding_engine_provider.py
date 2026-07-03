"""SECodeEngineProvider delegates to the software-engineering team's engines."""

from __future__ import annotations

import types

from software_engineering_team.coding_engine_provider import SECodeEngineProvider


def test_build_team_lead_routes_frontend(monkeypatch) -> None:
    import software_engineering_team.frontend_code_v2_team as fe

    captured: dict = {}

    class _FakeLead:
        def __init__(self, llm):
            captured["llm"] = llm

    monkeypatch.setattr(fe, "FrontendCodeV2TeamLead", _FakeLead)
    lead = SECodeEngineProvider().build_implementation_team_lead("frontend", "L")
    assert isinstance(lead, _FakeLead)
    assert captured["llm"] == "L"


def test_build_team_lead_routes_backend(monkeypatch) -> None:
    import software_engineering_team.backend_code_v2_team as be

    class _FakeLead:
        def __init__(self, llm):
            self.llm = llm

    monkeypatch.setattr(be, "BackendCodeV2TeamLead", _FakeLead)
    lead = SECodeEngineProvider().build_implementation_team_lead("backend", "L")
    assert isinstance(lead, _FakeLead)
    assert lead.llm == "L"


def test_quality_gate_methods_delegate(monkeypatch) -> None:
    import software_engineering_team.quality_gate_tools as qg

    monkeypatch.setattr(qg, "run_build_verification", lambda *a, **k: "build")
    monkeypatch.setattr(qg, "run_linting", lambda *a, **k: "lint")
    monkeypatch.setattr(qg, "run_code_review", lambda **k: "review")

    provider = SECodeEngineProvider()
    assert provider.run_build_verification("repo", "backend", "t1") == "build"
    assert provider.run_linting("repo", "t1", llm_getter=None) == "lint"
    assert provider.run_code_review(code="x", language="python") == "review"


def test_run_pr_code_review_builds_input_and_runs_agent(monkeypatch) -> None:
    import software_engineering_team.code_review_agent as cra

    class _FakeInput:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeAgent:
        def run(self, review_input, progress_callback=None):
            return types.SimpleNamespace(issues=[], review_input=review_input, cb=progress_callback)

    monkeypatch.setattr(cra, "CodeReviewInput", _FakeInput)
    monkeypatch.setattr(cra, "CodeReviewAgent", _FakeAgent)

    out = SECodeEngineProvider().run_pr_code_review(
        code="c",
        pre_numbered=True,
        task_description="d",
        task_requirements="r",
        language="python",
        progress_callback="cb",
    )
    assert out.issues == []
    assert out.cb == "cb"
    assert out.review_input.kw["language"] == "python"
