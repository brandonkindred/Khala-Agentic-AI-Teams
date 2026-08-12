"""Consolidated regression test: no SE-team temporal activity may write
JOB_STATUS_FAILED before the final Temporal retry attempt, and every one of
them must write it (and re-raise) on the final attempt.

Parametrized across all six "thin wrapper + outer except" activities in
software_engineering_team.temporal.activities. execute_coding_team_activity is
intentionally excluded: its own except block is a backstop for exceptions the
coding-team orchestrator itself never reached — the orchestrator owns the
job's terminal status on every normal exit path, an architecturally different
contract from the other six.

Adding a new activity that follows the same "if is_last_attempt(): update_job
(..., FAILED)" shape should mean adding one entry to CASES below, not a new
bespoke test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pytest
from temporalio.common import RetryPolicy


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def _fake_activity_info(attempt: int, maximum_attempts: int = 3):
    return type(
        "I",
        (),
        {"retry_policy": RetryPolicy(maximum_attempts=maximum_attempts), "attempt": attempt},
    )()


@dataclass
class ActivityCase:
    name: str
    # Given (monkeypatch, tmp_path, job_id), patch the activity's inner impl/call
    # to raise a distinctive RuntimeError and return a zero-arg callable that
    # invokes the activity with valid arguments.
    build_invoke: Callable[..., Callable[[], None]]


def _retry_failed_case(monkeypatch, tmp_path, job_id):
    from software_engineering_team.temporal import activities

    def boom(_, **kw):
        raise RuntimeError("contract: retry_failed")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", boom)
    return lambda: activities.retry_failed_activity(job_id)


def _frontend_code_v2_case(monkeypatch, tmp_path, job_id):
    from software_engineering_team.temporal import activities

    def boom(*a, **kw):
        raise RuntimeError("contract: frontend_code_v2")

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", boom)
    return lambda: activities.run_frontend_code_v2_activity(job_id, str(tmp_path), {"id": "t1"})


def _backend_code_v2_case(monkeypatch, tmp_path, job_id):
    from software_engineering_team.temporal import activities

    def boom(*a, **kw):
        raise RuntimeError("contract: backend_code_v2")

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", boom)
    return lambda: activities.run_backend_code_v2_activity(job_id, str(tmp_path), {"id": "t1"})


def _product_analysis_case(monkeypatch, tmp_path, job_id):
    from software_engineering_team.temporal import activities

    def boom(*a, **kw):
        raise RuntimeError("contract: product_analysis")

    monkeypatch.setattr(activities, "_run_product_analysis_impl", boom)
    return lambda: activities.run_product_analysis_activity(job_id, str(tmp_path), "spec")


def _parse_spec_case(monkeypatch, tmp_path, job_id):
    # No spec file on disk -> the real spec parser raises FileNotFoundError
    # inside _parse_spec_activity_body's outer try, same as the existing
    # test_parse_spec_activity_exception_path.
    from software_engineering_team.temporal import activities

    return lambda: activities.parse_spec_activity(
        job_id, str(tmp_path), trace_id="contract-parse-spec"
    )


def _plan_project_case(monkeypatch, tmp_path, job_id):
    from unittest.mock import MagicMock

    from software_engineering_team.temporal import activities

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setattr(
        "software_engineering_team.orchestrator._get_agents",
        lambda: {"architecture": MagicMock()},
    )

    def boom(*a, **kw):
        raise RuntimeError("contract: plan_project")

    monkeypatch.setattr("planning_team.orchestrator.run_workflow", boom)
    return lambda: activities.plan_project_activity(
        job_id,
        str(tmp_path),
        {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
    )


CASES = [
    ActivityCase("retry_failed_activity", _retry_failed_case),
    ActivityCase("run_frontend_code_v2_activity", _frontend_code_v2_case),
    ActivityCase("run_backend_code_v2_activity", _backend_code_v2_case),
    ActivityCase("run_product_analysis_activity", _product_analysis_case),
    ActivityCase("parse_spec_activity", _parse_spec_case),
    ActivityCase("plan_project_activity", _plan_project_case),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_final_attempt_marks_failed(case, monkeypatch, tmp_path) -> None:
    from software_engineering_team.shared import job_store as js

    job_id = f"{case.name}-final"
    js.create_job(job_id, repo_path=str(tmp_path))
    invoke = case.build_invoke(monkeypatch, tmp_path, job_id)

    with pytest.raises(Exception):
        invoke()

    job = js.get_job(job_id)
    assert job["status"] == js.JOB_STATUS_FAILED


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_non_final_attempt_does_not_mark_failed(case, monkeypatch, tmp_path) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    job_id = f"{case.name}-nonfinal"
    js.create_job(job_id, repo_path=str(tmp_path))
    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    invoke = case.build_invoke(monkeypatch, tmp_path, job_id)

    with pytest.raises(Exception):
        invoke()

    job = js.get_job(job_id)
    assert job["status"] != js.JOB_STATUS_FAILED
