"""Agent-level unit tests for the anatomy-conformant Planning agents.

Each agent is exercised directly (typed Input in, typed Output out), complementing
the phase-adapter tests in ``test_phases.py`` / ``test_document_production.py`` /
``test_temporal_activities.py``.
"""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_team.agents.discovery import DiscoveryAgent, DiscoveryInput  # noqa: E402
from planning_team.agents.document_production import (  # noqa: E402
    DocumentProductionAgent,
    DocumentProductionInput,
)
from planning_team.agents.intake import IntakeAgent, IntakeInput  # noqa: E402
from planning_team.agents.requirements import RequirementsAgent, RequirementsInput  # noqa: E402
from planning_team.agents.sub_agent_provisioning import (  # noqa: E402
    SubAgentProvisioningAgent,
    SubAgentProvisioningInput,
)
from planning_team.agents.synthesis import SynthesisAgent, SynthesisInput  # noqa: E402
from planning_team.models import ClientContext  # noqa: E402
from planning_team.tests.conftest import make_llm  # noqa: E402

# --- intake ------------------------------------------------------------------


def test_intake_agent_builds_context():
    out = IntakeAgent().run(
        IntakeInput(
            repo_path="/tmp/repo",
            client_name="Acme",
            initial_brief="brief",
            spec_content="spec",
        )
    )
    assert out.client_context.client_name == "Acme"
    assert out.client_context.raw_brief == "brief"
    assert out.client_context.raw_spec == "spec"
    assert out.repo_path == "/tmp/repo"
    assert out.initial_brief == "brief"
    assert out.spec_content == "spec"


def test_intake_agent_normalizes_missing_brief_spec():
    out = IntakeAgent().run(IntakeInput(repo_path="/tmp/repo"))
    assert out.initial_brief == ""
    assert out.spec_content == ""
    assert out.client_context.existing_artifacts == []


def test_intake_agent_keeps_existing_artifacts():
    out = IntakeAgent().run(IntakeInput(repo_path="/r", existing_artifacts=["a", "b"]))
    assert out.client_context.existing_artifacts == ["a", "b"]


# --- discovery ---------------------------------------------------------------


def test_discovery_agent_folds_into_client_context():
    llm = make_llm(
        '{"problem_summary": "Need X", "opportunity_statement": "Y",'
        ' "target_users": ["u1"], "success_criteria": ["c1"],'
        ' "tech_constraints": ["Rust"], "assumptions": ["a1"]}'
    )
    out = DiscoveryAgent().run(
        DiscoveryInput(
            client_context=ClientContext(client_name="Acme"),
            initial_brief="B",
            spec_content="S",
        ),
        llm,
    )
    assert out.client_context.problem_summary == "Need X"
    assert out.client_context.client_name == "Acme"  # prior context preserved
    assert out.client_context.target_users == ["u1"]
    assert out.client_context.tech_constraints == ["Rust"]
    assert out.discovery["opportunity_statement"] == "Y"


def test_discovery_agent_accepts_dict_client_context():
    llm = make_llm(
        '{"problem_summary": "P", "opportunity_statement": "",'
        ' "target_users": [], "success_criteria": [], "assumptions": []}'
    )
    out = DiscoveryAgent().run(
        DiscoveryInput(client_context={"client_name": "Acme"}, spec_content="S"), llm
    )
    assert out.client_context.client_name == "Acme"
    assert out.client_context.problem_summary == "P"


# --- requirements ------------------------------------------------------------


def test_requirements_agent_builds_questions_from_llm():
    llm = make_llm(
        '{"questions": [{"id": "req_1", "question_text": "RPO?", "category": "business",'
        ' "priority": "high", "options": [{"id": "opt_none", "label": "None", "is_default": true}]}]}'
    )
    out = RequirementsAgent().run(
        RequirementsInput(client_context=ClientContext(problem_summary="P")), llm
    )
    ids = {q.id for q in out.open_questions}
    assert "req_1" in ids


def test_requirements_agent_defaults_when_empty():
    llm = make_llm('{"questions": []}')
    out = RequirementsAgent().run(
        RequirementsInput(client_context=ClientContext(problem_summary="P")), llm
    )
    ids = {q.id for q in out.open_questions}
    assert "req_rpo_rto" in ids
    assert "req_deployment" in ids


# --- synthesis ---------------------------------------------------------------


def test_synthesis_agent_no_evidence():
    out = SynthesisAgent().run(SynthesisInput(client_context=ClientContext(client_name="Acme")))
    assert out.evidence is None
    assert out.evidence_attached is False
    assert out.client_context is None


def test_synthesis_agent_evidence_but_no_client_context():
    evidence = {"summary": "s", "insights": ["i"]}
    out = SynthesisAgent().run(
        SynthesisInput(client_context=None, market_research_evidence=evidence)
    )
    assert out.evidence_attached is True
    assert out.evidence == evidence
    assert out.client_context is None


def test_synthesis_agent_folds_into_constraints():
    evidence = {"summary": "Market is growing", "insights": ["i1"]}
    out = SynthesisAgent().run(
        SynthesisInput(
            client_context=ClientContext(client_name="Acme"), market_research_evidence=evidence
        )
    )
    assert out.evidence_attached is True
    assert out.client_context is not None
    assert out.client_context.constraints["market_research_summary"] == "Market is growing"
    assert out.client_context.constraints["market_research_insights"] == ["i1"]


def test_synthesis_agent_evidence_without_summary_or_insights():
    evidence = {"market_signals": []}  # truthy dict, but no summary/insights
    out = SynthesisAgent().run(
        SynthesisInput(
            client_context=ClientContext(client_name="Acme"), market_research_evidence=evidence
        )
    )
    assert out.evidence_attached is True
    assert out.client_context is None  # no fold-in branch taken


# --- sub_agent_provisioning --------------------------------------------------


def test_sub_agent_skips_without_gap():
    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path="/r", capability_gap=None)
    )
    assert out.sub_agent_blueprint is None
    assert out.error is None


def test_sub_agent_skips_without_repo_or_tools():
    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path="", capability_gap="gap")
    )
    assert out.sub_agent_blueprint is None
    assert out.error is None


def test_sub_agent_start_failed(tmp_path):
    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path=str(tmp_path), capability_gap="gap"),
        start_build_fn=lambda **kw: None,
        wait_build_fn=lambda **kw: {},
    )
    assert out.sub_agent_blueprint is None
    assert out.error == "AI Systems build start failed"
    assert (tmp_path / "plan" / "sub_agent_spec.md").exists()


def test_sub_agent_success_dict_blueprint(tmp_path):
    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path=str(tmp_path), capability_gap="gap"),
        start_build_fn=lambda **kw: "job1",
        wait_build_fn=lambda **kw: {"status": "completed", "blueprint": {"name": "bp"}},
    )
    assert out.sub_agent_blueprint == {"name": "bp"}
    assert out.error is None


def test_sub_agent_success_model_blueprint(tmp_path):
    class _BP:
        def model_dump(self):
            return {"dumped": True}

    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path=str(tmp_path), capability_gap="gap"),
        start_build_fn=lambda **kw: "job1",
        wait_build_fn=lambda **kw: {"status": "completed", "blueprint": _BP()},
    )
    assert out.sub_agent_blueprint == {"dumped": True}


def test_sub_agent_failed_build_reports_error(tmp_path):
    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(repo_path=str(tmp_path), capability_gap="gap"),
        start_build_fn=lambda **kw: "job1",
        wait_build_fn=lambda **kw: {"status": "failed", "error": "boom"},
    )
    assert out.sub_agent_blueprint is None
    assert out.error == "boom"


# --- document_production -----------------------------------------------------


def test_document_production_agent_no_pra_uses_initial_spec(tmp_path):
    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=False,
        )
    )
    hp = out.handoff_package
    assert hp.validated_spec_path.endswith("initial_spec.md")
    assert hp.client_context is not None
    assert "handoff_package" in out.artifacts
    assert "client_context_document_path" in out.artifacts
    assert "initial_spec_path" in out.artifacts


def test_document_production_agent_pra_completed_path(tmp_path):
    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=True,
        ),
        run_pra=lambda **kw: "job1",
        wait_pra=lambda **kw: {"status": "completed", "validated_spec_path": None},
    )
    hp = out.handoff_package
    assert hp.validated_spec_path.endswith("validated_spec.md")
    assert hp.prd_path.endswith("product_requirements_document.md")


def test_document_production_agent_runs_architecture_step(tmp_path):
    captured = {}

    def _arch(**kw):
        captured.update(kw)
        return "arch overview"

    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=False,
        ),
        run_architecture_fn=_arch,
    )
    assert out.handoff_package.architecture_overview == "arch overview"
    # normalized client context is passed to the architecture tool as a dict
    assert captured["client_context"]["client_name"] == "Acme"


def test_document_production_agent_no_client_context(tmp_path):
    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=None,
            initial_brief="Just a brief",
            use_product_analysis=False,
        )
    )
    hp = out.handoff_package
    assert hp.client_context is None
    assert "client_context_document_path" not in out.artifacts  # doc not written


def test_document_production_agent_pra_no_job_id(tmp_path):
    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=True,
        ),
        run_pra=lambda **kw: None,  # start failed
        wait_pra=lambda **kw: {"status": "completed"},
    )
    # No PRA job -> validated_spec_path never resolved from PRA output.
    assert out.handoff_package.validated_spec_path is None


def test_document_production_agent_pra_not_completed(tmp_path):
    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=True,
        ),
        run_pra=lambda **kw: "job1",
        wait_pra=lambda **kw: {"status": "failed", "error": "PRA blew up"},
    )
    assert out.handoff_package.validated_spec_path is None
    assert out.handoff_package.prd_path is None


def test_document_production_agent_architecture_exception_is_swallowed(tmp_path):
    def _boom(**kw):
        raise RuntimeError("arch failed")

    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=ClientContext(client_name="Acme"),
            spec_content="# Spec",
            use_product_analysis=False,
        ),
        run_architecture_fn=_boom,
    )
    # Architecture failure is logged and swallowed; handoff still produced.
    assert out.handoff_package.architecture_overview is None
    assert "handoff_package" in out.artifacts


def test_document_production_agent_architecture_with_none_client_context(tmp_path):
    captured = {}

    def _arch(**kw):
        captured.update(kw)
        return "overview"

    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=str(tmp_path),
            client_context=None,
            initial_brief="brief",
            use_product_analysis=False,
        ),
        run_architecture_fn=_arch,
    )
    assert out.handoff_package.architecture_overview == "overview"
    assert captured["client_context"] is None  # no context -> None passed through
