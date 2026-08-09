"""Tests that ``run_orchestrator``/``run_failed_tasks`` bind one ``trace_id`` shared by every
phase (Discovery, Design, Execution, Integration) of a job run.

Mirrors ``llm_service/tests/test_attribution.py``'s style for a contextvar bound around a phase.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import orchestrator
import pytest

from shared.dev_models.models import ProductRequirements
from shared.observability import current_trace_id


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def test_run_orchestrator_binds_same_trace_id_across_all_four_phases(tmp_path: Path) -> None:
    """Discovery, Design (Planning), Execution, and Integration all see one shared trace id."""
    from planning_adapter import PlanningAdapterResult

    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    job_id = "test-trace-id-all-phases"

    seen_trace_ids: dict = {}

    mock_pra_result = MagicMock()
    mock_pra_result.success = True
    mock_pra_result.final_spec_content = "# Test\n\nSpec."
    mock_pra_result.iterations = 1
    mock_pra_agent = MagicMock()

    def fake_pra_run_workflow(*a, **kw):
        seen_trace_ids["discovery"] = current_trace_id()
        return mock_pra_result

    mock_pra_agent.run_workflow.side_effect = fake_pra_run_workflow

    adapter_result = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="Test", description="Desc", acceptance_criteria=[], constraints=[]
        ),
        project_overview={"goals": "Ship", "features_and_functionality_doc": "API"},
        open_questions=[],
        assumptions=[],
        hierarchy=None,
        final_spec_content="# Test\n\nSpec.",
        architecture_overview="Backend FastAPI; frontend Angular.",
    )

    def fake_run_planning_workflow(*a, **kw):
        seen_trace_ids["design"] = current_trace_id()
        return {
            "success": True,
            "handoff_package": {"architecture_overview": "Backend FastAPI; frontend Angular."},
            "failure_reason": None,
        }

    def fake_adapt_planning_result(*a, **kw):
        return adapter_result

    def fake_run_coding_team(jid, repo_path, plan_input, **kwargs):
        seen_trace_ids["execution"] = current_trace_id()
        if kwargs.get("update_job_fn"):
            kwargs["update_job_fn"](status="completed", phase="completed")

    def fake_emit_metrics(jid):
        seen_trace_ids["integration_metrics"] = current_trace_id()

    def fake_finalize_snapshot(jid):
        seen_trace_ids["integration_finalize"] = current_trace_id()

    mock_agents = {
        "architecture": MagicMock(),
        "devops": MagicMock(),
        "backend": MagicMock(),
        "frontend": MagicMock(),
        "frontend_code_v2": MagicMock(),
        "git_setup": MagicMock(),
        "integration": MagicMock(),
        "acceptance_verifier": MagicMock(),
        "qa": MagicMock(),
        "security": MagicMock(),
        "accessibility": MagicMock(),
        "code_review": MagicMock(),
        "dbc_comments": MagicMock(),
        "documentation": MagicMock(),
    }

    with patch("orchestrator.update_job"):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            with patch(
                "software_engineering_team.spec_parser.parse_spec_with_llm",
                return_value=ProductRequirements(
                    title="Test", description="Desc", acceptance_criteria=[], constraints=[]
                ),
            ):
                with patch(
                    "software_engineering_team.product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
                    return_value=mock_pra_agent,
                ):
                    with patch(
                        "planning_team.orchestrator.run_workflow",
                        side_effect=fake_run_planning_workflow,
                    ):
                        with patch(
                            "software_engineering_team.planning_adapter.adapt_planning_result",
                            side_effect=fake_adapt_planning_result,
                        ):
                            with (
                                patch(
                                    "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
                                    side_effect=fake_run_coding_team,
                                ),
                                patch(
                                    "software_engineering_team.shared.planning_audit.record_se_planning_run"
                                ),
                                patch(
                                    "orchestrator._emit_coding_team_metrics",
                                    side_effect=fake_emit_metrics,
                                ),
                                patch(
                                    "orchestrator._finalize_from_coding_snapshot",
                                    side_effect=fake_finalize_snapshot,
                                ),
                            ):
                                orchestrator.run_orchestrator(job_id, str(tmp_path))

    assert current_trace_id() == ""  # unbound again once run_orchestrator returns
    assert set(seen_trace_ids) == {
        "discovery",
        "design",
        "execution",
        "integration_metrics",
        "integration_finalize",
    }
    ids = set(seen_trace_ids.values())
    assert len(ids) == 1, f"expected one shared trace id across all phases, got {seen_trace_ids}"
    assert next(iter(ids))  # non-empty


def test_run_orchestrator_honors_caller_supplied_trace_id(tmp_path: Path) -> None:
    """A caller-supplied ``trace_id`` (e.g. from a Temporal activity) is bound verbatim."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    seen = {}

    def fake_resolve_spec_source(*a, **kw):
        seen["trace_id"] = current_trace_id()
        return None  # short-circuits run_orchestrator right after Discovery starts

    with patch("orchestrator.update_job"):
        with patch("orchestrator.resolve_spec_source", side_effect=fake_resolve_spec_source):
            orchestrator.run_orchestrator(
                "test-trace-id-explicit", str(tmp_path), trace_id="caller-supplied-id"
            )

    assert seen["trace_id"] == "caller-supplied-id"
    assert current_trace_id() == ""


def test_run_orchestrator_rejects_falsy_explicit_trace_id_by_generating_one(
    tmp_path: Path,
) -> None:
    """An empty-string ``trace_id`` (Temporal's blank default) falls back to a generated one."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    seen = {}

    def fake_resolve_spec_source(*a, **kw):
        seen["trace_id"] = current_trace_id()
        return None

    with patch("orchestrator.update_job"):
        with patch("orchestrator.resolve_spec_source", side_effect=fake_resolve_spec_source):
            orchestrator.run_orchestrator("test-trace-id-blank", str(tmp_path), trace_id="")

    assert seen["trace_id"]  # non-empty, generated
    assert seen["trace_id"] != ""


def test_run_failed_tasks_binds_a_trace_id_around_the_retry(tmp_path: Path) -> None:
    """``run_failed_tasks`` (Execution/Integration re-entry) also binds a shared trace id."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = "test-trace-id-retry"
    create_job(job_id, repo_path=str(tmp_path))
    update_job(job_id, task_graph_snapshot=[{"id": "t1", "status": "failed"}])
    seen = {}

    def fake_run_coding_and_finalize(jid, path, plan_input, retry_failed=False):
        seen["trace_id"] = current_trace_id()

    with patch("orchestrator._run_coding_and_finalize", side_effect=fake_run_coding_and_finalize):
        orchestrator.run_failed_tasks(job_id)

    assert seen["trace_id"]
    assert current_trace_id() == ""
