"""Unit tests for AgentProvisioningWorkflow.

The workflow is exercised by stubbing `workflow.execute_activity` so we
never need a live Temporal worker — we just verify the workflow's
control flow (skip / resume, fan-out, failure → compensation, etc.).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# AgentProvisioningWorkflow — direct .run() invocation
#
# `workflow.execute_activity` is stubbed so we don't need a real
# Temporal env. The workflow's control-flow assertions are what we care about.
# ---------------------------------------------------------------------------


class _ExecActivityStub:
    """Callable stub that records every call and returns canned responses
    keyed by the activity function's name."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls = []

    async def __call__(self, activity_fn, *args, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        self.calls.append({"name": name, "args": kwargs.get("args"), "kwargs": kwargs})
        if name in self.responses:
            resp = self.responses[name]
            if isinstance(resp, BaseException):
                raise resp
            if callable(resp):
                return resp(self.calls[-1])
            return resp
        return None


def _build_manifest_yaml(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: postgresql
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
  - name: redis
    provisioner: redis_provisioner
    config: {key_prefix: "k:"}
""",
        encoding="utf-8",
    )
    return str(f)


@pytest.mark.asyncio
async def test_workflow_happy_path(tmp_path, monkeypatch) -> None:
    """Happy path runs setup → credentials → per-tool provision → audit → docs → deliver."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"},
                    "redis": {"tool_name": "redis", "username": "u", "password": "p"},
                },
            },
            "provision_tool_activity": lambda call: {
                "tool_name": call["args"][2],
                "success": True,
                "provisioner_key": "x",
            },
            "record_account_provisioning_activity": {"success": True, "tool_results": []},
            "audit_activity": {"passed": True, "verifications": []},
            "documentation_activity": {"success": True, "onboarding": {"summary": "s"}},
            "deliver_activity": {"success": True, "error": None},
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        workflow = wf.AgentProvisioningWorkflow()
        await workflow.run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "setup_activity" in fn_names
    assert "credentials_activity" in fn_names
    # Two tools → two provision activities.
    assert fn_names.count("provision_tool_activity") == 2
    assert "record_account_provisioning_activity" in fn_names
    assert "audit_activity" in fn_names
    assert "documentation_activity" in fn_names
    assert "deliver_activity" in fn_names


@pytest.mark.asyncio
async def test_workflow_compensates_on_tool_failure(tmp_path) -> None:
    """When a tool fails, succeeded tools are compensated and the job is marked failed."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    # One tool succeeds, the other raises.
    def provision_responder(call):
        tool_name = call["args"][2]
        if tool_name == "postgresql":
            return {
                "tool_name": "postgresql",
                "success": True,
                "provisioner_key": "postgres_provisioner",
            }
        raise RuntimeError("redis exploded")

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Tool provisioning failed"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    # Compensate was invoked
    assert "compensate_activity" in fn_names
    assert fn_names.count("mark_job_failed_activity") == 1


@pytest.mark.asyncio
async def test_workflow_skips_provisioning_when_resumed(tmp_path) -> None:
    """Resume with prior successful account_provisioning skips per-tool fan-out."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "audit_activity": {"passed": True, "verifications": []},
            "documentation_activity": {"success": True, "onboarding": {"summary": "s"}},
            "deliver_activity": {"success": True, "error": None},
        }
    )

    prior = {
        "account_provisioning": {
            "tool_results": [
                {
                    "tool_name": "postgresql",
                    "success": True,
                    "provisioner_key": "postgres_provisioner",
                },
                {
                    "tool_name": "redis",
                    "success": True,
                    "provisioner_key": "redis_provisioner",
                },
            ]
        }
    }

    with patch.object(wf.workflow, "execute_activity", new=stub):
        await wf.AgentProvisioningWorkflow().run(
            "job-1",
            "agent-1",
            manifest_path,
            skip_phases=["account_provisioning"],
            prior_results=prior,
        )

    fn_names = [c["name"] for c in stub.calls]
    # No per-tool provisioning happened
    assert "provision_tool_activity" not in fn_names
    assert "record_account_provisioning_activity" not in fn_names
    # Compensation is skipped because everything in prior was successful
    assert "compensate_activity" not in fn_names


@pytest.mark.asyncio
async def test_workflow_resume_rejects_tool_set_mismatch(tmp_path) -> None:
    """Restored account_provisioning must match the current manifest tool set."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "mark_job_failed_activity": None,
        }
    )
    prior = {
        "account_provisioning": {
            "tool_results": [
                {
                    "tool_name": "postgresql",
                    "success": True,
                    "provisioner_key": "postgres_provisioner",
                },
            ]
        }
    }
    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Cannot restore account_provisioning"):
            await wf.AgentProvisioningWorkflow().run(
                "job-1",
                "agent-1",
                manifest_path,
                skip_phases=["account_provisioning"],
                prior_results=prior,
            )

    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]


@pytest.mark.asyncio
async def test_workflow_resume_with_prior_failed_tools_compensates(tmp_path) -> None:
    """Resume restoring a prior phase that includes failed tools still compensates."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    prior = {
        "account_provisioning": {
            "tool_results": [
                {
                    "tool_name": "postgresql",
                    "success": True,
                    "provisioner_key": "postgres_provisioner",
                },
                {
                    "tool_name": "redis",
                    "success": False,
                    "error": "ack",
                    "provisioner_key": "redis_provisioner",
                },
            ]
        }
    }

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Tool provisioning failed"):
            await wf.AgentProvisioningWorkflow().run(
                "job-1",
                "agent-1",
                manifest_path,
                skip_phases=["account_provisioning"],
                prior_results=prior,
            )

    fn_names = [c["name"] for c in stub.calls]
    assert "compensate_activity" in fn_names
    assert "mark_job_failed_activity" in fn_names


@pytest.mark.asyncio
async def test_workflow_handles_non_dict_provision_results(tmp_path) -> None:
    """A provision_tool_activity result that isn't a dict (e.g. None) → failure path."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    def provision_responder(call):
        tool_name = call["args"][2]
        # Return weird non-dict for one tool
        if tool_name == "redis":
            return None
        return {"tool_name": "postgresql", "success": True, "provisioner_key": "x"}

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": None},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Tool provisioning failed"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)


@pytest.mark.asyncio
async def test_workflow_handles_dict_failure_results(tmp_path) -> None:
    """A provision_tool_activity result dict with success=False → failure path."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    def provision_responder(call):
        tool_name = call["args"][2]
        if tool_name == "redis":
            return {"tool_name": "redis", "success": False, "error": "redis down"}
        return {"tool_name": "postgresql", "success": True, "provisioner_key": "x"}

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": None},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="redis down"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)


@pytest.mark.asyncio
async def test_workflow_marks_failed_on_audit_error(tmp_path) -> None:
    """Non-tool phase exceptions must persist terminal failure before re-raising."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": ["postgresql", "redis"],
            "credentials_activity": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": lambda call: {
                "tool_name": call["args"][2],
                "success": True,
                "provisioner_key": "x",
            },
            "record_account_provisioning_activity": {"success": True, "tool_results": []},
            "audit_activity": RuntimeError("audit boom"),
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="audit boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "record_account_provisioning_activity" in fn_names
    assert fn_names.count("mark_job_failed_activity") == 1
