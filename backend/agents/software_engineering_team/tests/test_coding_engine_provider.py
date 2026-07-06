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


def test_run_pr_code_review_legacy_code_mode(monkeypatch) -> None:
    """Diff-hunk (``code=``) mode: builds a code-backed input, forwards no reader."""
    import software_engineering_team.code_review_agent as cra

    class _FakeAgent:
        def run(self, review_input, **kwargs):
            return types.SimpleNamespace(issues=[], review_input=review_input, kwargs=kwargs)

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
    assert out.kwargs["progress_callback"] == "cb"
    # No repo_reader supplied -> not forwarded (keeps duck-typed stubs working).
    assert "repo_reader" not in out.kwargs
    assert out.review_input.code == "c"
    assert out.review_input.files is None
    assert out.review_input.pre_numbered is True
    assert out.review_input.language == "python"


def test_run_pr_code_review_whole_file_mode_forwards_reader(monkeypatch) -> None:
    """Whole-file (``files=``) mode: builds a files-backed input and forwards the reader."""
    import software_engineering_team.code_review_agent as cra

    class _FakeAgent:
        def run(self, review_input, **kwargs):
            return types.SimpleNamespace(issues=[], review_input=review_input, kwargs=kwargs)

    monkeypatch.setattr(cra, "CodeReviewAgent", _FakeAgent)

    reader = object()
    out = SECodeEngineProvider().run_pr_code_review(
        files={"a.py": "x = 1\n"},
        pre_numbered=False,
        task_description="d",
        task_requirements="r",
        language="python",
        progress_callback="cb",
        repo_reader=reader,
    )
    assert out.review_input.files == {"a.py": "x = 1\n"}
    # files takes precedence: no code blob leaks through.
    assert out.review_input.code == ""
    assert out.review_input.pre_numbered is False
    assert out.kwargs["repo_reader"] is reader
