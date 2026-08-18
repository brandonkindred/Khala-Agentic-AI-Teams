"""SECodeEngineProvider delegates to the software-engineering team's engines."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from software_engineering_team.coding_engine_provider import SECodeEngineProvider


class _FakeReplacedContentAgent:
    """Stub ``CodeReviewAgent`` that just echoes back the ``review_input`` it
    was run with, for tests asserting on how ``replaced_content`` was built."""

    def __init__(self, llm_client=None, *, force_in_process=False):
        pass

    def run(self, review_input, **kwargs):
        return types.SimpleNamespace(issues=[], review_input=review_input)


@pytest.fixture
def fake_code_review_agent(monkeypatch):
    """Monkeypatch ``CodeReviewAgent`` with ``_FakeReplacedContentAgent``."""
    import software_engineering_team.code_review_agent as cra

    monkeypatch.setattr(cra, "CodeReviewAgent", _FakeReplacedContentAgent)


def test_import_bootstraps_se_team_dir_on_syspath() -> None:
    """Importing the provider puts the SE team dir on ``sys.path``.

    SE's engines (``quality_gate_tools.run_code_review``) use bare team-local
    imports like ``from code_review_agent import ...`` that resolve only with the
    SE team directory on ``sys.path``. Out-of-package composition roots (the
    standalone coding-team service, the coding_team Temporal worker) reach these
    engines solely through this provider and never import ``software_engineering_team.api``,
    which is what otherwise bootstraps that path. Guard the invariant so the
    ``No module named 'code_review_agent'`` regression that stalled the coding
    pipeline cannot return.
    """
    team_dir = Path(__file__).resolve().parent.parent
    assert str(team_dir) in sys.path
    assert str(team_dir / "architect_agents") in sys.path
    # The bare team-local name the engines depend on must now be discoverable.
    assert (team_dir / "code_review_agent").is_dir()


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
    assert provider.run_code_review(files={"x.py": "x"}, language="python") == "review"


def test_run_pr_code_review_whole_file_mode_forwards_reader(monkeypatch) -> None:
    """Whole-file (``files=``) mode: builds a files-backed input, forwards the reader,
    and forces the in-process coordinator so the live reader is actually used."""
    import software_engineering_team.code_review_agent as cra

    class _FakeAgent:
        def __init__(self, llm_client=None, *, force_in_process=False):
            self.force_in_process = force_in_process

        def run(self, review_input, **kwargs):
            return types.SimpleNamespace(
                issues=[],
                review_input=review_input,
                kwargs=kwargs,
                force_in_process=self.force_in_process,
            )

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
    assert out.review_input.pre_numbered is False
    assert out.kwargs["repo_reader"] is reader
    # A live GitHub reader cannot cross the Temporal boundary, so the provider
    # forces the in-process path whenever a reader is supplied.
    assert out.force_in_process is True


def test_run_pr_code_review_forwards_replaced_content(fake_code_review_agent) -> None:
    """``replaced_content`` (the diff-derived before-image) is forwarded to
    ``CodeReviewInput`` unchanged, additively alongside ``files``."""
    before = {"a.py": "x = 0\n"}
    out = SECodeEngineProvider().run_pr_code_review(
        files={"a.py": "x = 1\n"},
        pre_numbered=False,
        task_description="d",
        task_requirements="r",
        language="python",
        progress_callback="cb",
        replaced_content=before,
    )
    assert out.review_input.replaced_content == before
    assert out.review_input.files == {"a.py": "x = 1\n"}


def test_run_pr_code_review_defaults_replaced_content_to_none(fake_code_review_agent) -> None:
    """Omitting ``replaced_content`` behaves exactly as before its introduction."""
    out = SECodeEngineProvider().run_pr_code_review(
        files={"a.py": "x = 1\n"},
        pre_numbered=False,
        task_description="d",
        task_requirements="r",
        language="python",
        progress_callback="cb",
    )
    assert out.review_input.replaced_content is None


def test_classify_issue_scope_delegates_to_scope_classifier(monkeypatch) -> None:
    """Delegates straight through to ``scope_classifier.classify_scope``, passing
    ``findings`` unchanged and adapting ``changed_context``/``task_description``
    into a ``CodeReviewInput``. The verify-model client comes from
    ``model_resolution.resolve_code_review_verify_client`` -- the same
    Ollama-pinned resolver the sibling verification passes use -- not a bare
    ``get_client`` call."""
    import software_engineering_team.code_review_agent.model_resolution as model_resolution
    import software_engineering_team.code_review_agent.scope_classifier as sc

    captured: dict = {}
    sentinel = ["verdict-a", "verdict-b"]

    def _fake_classify_scope(issues, *, llm=None, input_data=None, **kwargs):
        captured["issues"] = issues
        captured["llm"] = llm
        captured["input_data"] = input_data
        return sentinel

    def _fake_resolve_client():
        return "the-pinned-client"

    monkeypatch.setattr(sc, "classify_scope", _fake_classify_scope)
    monkeypatch.setattr(model_resolution, "resolve_code_review_verify_client", _fake_resolve_client)

    findings = ["f1", "f2"]
    out = SECodeEngineProvider().classify_issue_scope(
        findings, {"a.py": "x = 1\n"}, "review this PR"
    )

    assert out is sentinel
    assert captured["issues"] is findings
    assert captured["llm"] == "the-pinned-client"
    assert captured["input_data"].files == {"a.py": "x = 1\n"}
    assert captured["input_data"].task_description == "review this PR"


@pytest.mark.parametrize("changed_context", [None, {}])
def test_classify_issue_scope_empty_changed_context_omits_input_data(
    monkeypatch, changed_context
) -> None:
    """A falsy ``changed_context`` (``None`` or ``{}``) never constructs a
    ``CodeReviewInput`` -- which would raise on empty ``files`` -- so
    ``classify_scope`` receives ``input_data=None``."""
    import software_engineering_team.code_review_agent.model_resolution as model_resolution
    import software_engineering_team.code_review_agent.scope_classifier as sc

    captured: dict = {}

    def _fake_classify_scope(issues, *, llm=None, input_data=None, **kwargs):
        captured["input_data"] = input_data
        return []

    monkeypatch.setattr(sc, "classify_scope", _fake_classify_scope)
    monkeypatch.setattr(
        model_resolution, "resolve_code_review_verify_client", lambda: "the-pinned-client"
    )

    SECodeEngineProvider().classify_issue_scope(["f1"], changed_context, "desc")

    assert captured["input_data"] is None


def test_classify_issue_scope_client_resolution_failure_degrades_to_none_llm(monkeypatch) -> None:
    """A client-resolution failure (e.g. no LLM provider configured) never
    propagates -- ``classify_scope`` receives ``llm=None`` and degrades every
    finding to "unknown" itself, matching its own never-raises contract."""
    import software_engineering_team.code_review_agent.model_resolution as model_resolution
    import software_engineering_team.code_review_agent.scope_classifier as sc

    captured: dict = {}

    def _fake_classify_scope(issues, *, llm=None, input_data=None, **kwargs):
        captured["llm"] = llm
        return []

    def _raising_resolve_client():
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(sc, "classify_scope", _fake_classify_scope)
    monkeypatch.setattr(
        model_resolution, "resolve_code_review_verify_client", _raising_resolve_client
    )

    out = SECodeEngineProvider().classify_issue_scope(["f1"], None, "desc")

    assert out == []
    assert captured["llm"] is None
