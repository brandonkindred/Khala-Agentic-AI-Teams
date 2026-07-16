"""Drive run_pipeline with gates enabled to cover the rewrite loop branches.

Tests:
* All gates PASS → status=PASS
* Gates FAIL on iter 1, PASS on iter 2 → status=PASS
* Gates never pass → status=NEEDS_HUMAN_REVIEW
"""

from __future__ import annotations

from pathlib import Path


def _make_plan():
    from agents.blogging.shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningPhaseResult,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    return PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=1,
        parse_retry_count=0,
        planning_wall_ms_total=10.0,
    )


def _stub_writer_class():
    from agents.blogging.blog_writer_agent.models import WriterOutput

    class _StubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return WriterOutput(draft="# Draft\nBody.")

        def revise(self, *a, **kw):
            return WriterOutput(draft="# Revised\nBody.")

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft="# Revised\nBody.")

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return ""

    return _StubWriter


def _stub_editor_class():
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput

    class _StubEditor:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return CopyEditorOutput(approved=True, summary="ok", feedback_items=[])

    return _StubEditor


def _stub_compliance(status: str = "PASS"):
    from agents.blogging.blog_compliance_agent.models import ComplianceReport

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return ComplianceReport(status=status, violations=[], required_fixes=[], notes="ok")

    return _Stub


def _stub_factcheck(claims_status: str = "PASS", risk_status: str = "PASS"):
    from agents.blogging.blog_fact_check_agent.models import FactCheckReport

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return FactCheckReport(
                claims_status=claims_status,
                risk_status=risk_status,
                risk_flags=[] if risk_status == "PASS" else ["claim X"],
                required_disclaimers=[],
                unverified_claims=[],
                claims=[],
            )

    return _Stub


class _ValidatorStub:
    def __init__(self, status: str = "PASS"):
        self.status = status
        self.checks = []

    def model_dump(self):
        return {"status": self.status, "checks": []}


def _common_v2_setup(monkeypatch, validator_status: str = "PASS"):
    """Apply the standard set of monkeypatches to v2 for gate tests."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "ok")
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda *a, **kw: "brand")
    monkeypatch.setattr(v2, "BlogWriterAgent", _stub_writer_class())
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", _stub_editor_class())
    monkeypatch.setattr(
        v2,
        "run_validators_from_work_dir",
        lambda wd: _ValidatorStub(status=validator_status),
    )
    return v2


def test_run_pipeline_with_gates_all_pass(monkeypatch, tmp_path: Path) -> None:
    """All gates pass on iteration 1 → status=PASS."""
    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogComplianceAgent", _stub_compliance("PASS"))
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _stub_factcheck("PASS", "PASS"))

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    _, _, status = v2.run_pipeline(
        brief,
        work_dir=tmp_path / "wd",
        run_gates=True,
        max_rewrite_iterations=2,
        draft_editor_iterations=1,
    )
    assert status == "PASS"


def test_run_pipeline_with_gates_exhausts_iterations(monkeypatch, tmp_path: Path) -> None:
    """Gates never pass → status=NEEDS_HUMAN_REVIEW after max_rewrite_iterations."""
    v2 = _common_v2_setup(monkeypatch, validator_status="FAIL")
    monkeypatch.setattr(v2, "BlogComplianceAgent", _stub_compliance("FAIL"))
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _stub_factcheck("FAIL", "FAIL"))

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    _, _, status = v2.run_pipeline(
        brief,
        work_dir=tmp_path / "wd",
        run_gates=True,
        max_rewrite_iterations=1,
        draft_editor_iterations=1,
    )
    assert status == "NEEDS_HUMAN_REVIEW"


def test_run_pipeline_with_gates_pass_after_one_rewrite(monkeypatch, tmp_path: Path) -> None:
    """Gates fail on iter 1, pass on iter 2."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "ok")
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda *a, **kw: "brand")
    monkeypatch.setattr(v2, "BlogWriterAgent", _stub_writer_class())
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", _stub_editor_class())

    # Validator: FAIL first, PASS second
    state = {"i": 0}

    def validator_factory(wd):
        state["i"] += 1
        return _ValidatorStub(status="PASS" if state["i"] >= 2 else "FAIL")

    monkeypatch.setattr(v2, "run_validators_from_work_dir", validator_factory)

    from agents.blogging.blog_compliance_agent.models import ComplianceReport
    from agents.blogging.blog_fact_check_agent.models import FactCheckReport

    compliance_state = {"i": 0}

    class _Compliance:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            compliance_state["i"] += 1
            return ComplianceReport(
                status="PASS" if compliance_state["i"] >= 2 else "FAIL",
                violations=[],
                required_fixes=[] if compliance_state["i"] >= 2 else ["fix me"],
                notes=None,
            )

    fc_state = {"i": 0}

    class _FactCheck:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            fc_state["i"] += 1
            return FactCheckReport(
                claims_status="PASS",
                risk_status="PASS" if fc_state["i"] >= 2 else "FAIL",
                risk_flags=[] if fc_state["i"] >= 2 else ["x"],
                required_disclaimers=[],
                unverified_claims=[],
                claims=[],
            )

    monkeypatch.setattr(v2, "BlogComplianceAgent", _Compliance)
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _FactCheck)

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    _, _, status = v2.run_pipeline(
        brief,
        work_dir=tmp_path / "wd",
        run_gates=True,
        max_rewrite_iterations=3,
        draft_editor_iterations=1,
    )
    assert status == "PASS"


def _raising_gate(exc: Exception):
    """A gate-agent stub class whose ``run`` raises ``exc``."""

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            raise exc

    return _Stub


def _run_gated_pipeline(v2, tmp_path):
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    return v2.run_pipeline(
        brief,
        work_dir=tmp_path / "wd",
        run_gates=True,
        max_rewrite_iterations=1,
        draft_editor_iterations=1,
    )


def test_gate_factcheck_transient_error_propagates_unwrapped(monkeypatch, tmp_path: Path) -> None:
    """A transient LLM error from the fact-check gate propagates unwrapped (for Temporal retry)."""
    import pytest

    from llm_service import LLMTemporaryError

    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogComplianceAgent", _stub_compliance("PASS"))
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _raising_gate(LLMTemporaryError("503")))

    with pytest.raises(LLMTemporaryError):
        _run_gated_pipeline(v2, tmp_path)


def test_gate_factcheck_generic_error_maps_to_factcheckerror(monkeypatch, tmp_path: Path) -> None:
    """A non-transient error from the fact-check gate maps to FactCheckError."""
    import pytest
    from agents.blogging.shared.errors import FactCheckError

    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogComplianceAgent", _stub_compliance("PASS"))
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _raising_gate(RuntimeError("boom")))

    with pytest.raises(FactCheckError):
        _run_gated_pipeline(v2, tmp_path)


def test_gate_compliance_generic_error_maps_to_complianceerror(monkeypatch, tmp_path: Path) -> None:
    """A non-transient error from the compliance gate maps to ComplianceError."""
    import pytest
    from agents.blogging.shared.errors import ComplianceError

    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _stub_factcheck("PASS", "PASS"))
    monkeypatch.setattr(v2, "BlogComplianceAgent", _raising_gate(RuntimeError("boom")))

    with pytest.raises(ComplianceError):
        _run_gated_pipeline(v2, tmp_path)


def test_both_gates_invoked_when_parallelized(monkeypatch, tmp_path: Path) -> None:
    """Both gates run (concurrently) and their PASS reports combine to status=PASS."""
    from agents.blogging.blog_compliance_agent.models import ComplianceReport
    from agents.blogging.blog_fact_check_agent.models import FactCheckReport

    calls = {"fact": 0, "compliance": 0}

    class _CountingFact:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            calls["fact"] += 1
            return FactCheckReport(
                claims_status="PASS",
                risk_status="PASS",
                risk_flags=[],
                required_disclaimers=[],
                unverified_claims=[],
                claims=[],
            )

    class _CountingCompliance:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            calls["compliance"] += 1
            return ComplianceReport(status="PASS", violations=[], required_fixes=[], notes="ok")

    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _CountingFact)
    monkeypatch.setattr(v2, "BlogComplianceAgent", _CountingCompliance)

    _, _, status = _run_gated_pipeline(v2, tmp_path)
    assert status == "PASS"
    assert calls == {"fact": 1, "compliance": 1}


def test_gate_failure_drains_other_gate(monkeypatch, tmp_path: Path) -> None:
    """When one gate raises, the other still runs to completion before the error
    propagates — no abandoned worker that could overwrite a later attempt's artifact."""
    import pytest
    from agents.blogging.blog_compliance_agent.models import ComplianceReport

    from llm_service import LLMTemporaryError

    compliance_calls = {"n": 0}

    class _CountingCompliance:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            compliance_calls["n"] += 1
            return ComplianceReport(status="PASS", violations=[], required_fixes=[], notes="ok")

    v2 = _common_v2_setup(monkeypatch, validator_status="PASS")
    monkeypatch.setattr(v2, "BlogFactCheckAgent", _raising_gate(LLMTemporaryError("503")))
    monkeypatch.setattr(v2, "BlogComplianceAgent", _CountingCompliance)

    with pytest.raises(LLMTemporaryError):
        _run_gated_pipeline(v2, tmp_path)
    # Compliance ran to completion even though fact-check failed (drain, not fast-fail).
    assert compliance_calls["n"] == 1
