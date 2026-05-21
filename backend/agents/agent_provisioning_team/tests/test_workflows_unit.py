"""Unit tests for AgentProvisioningWorkflowV2 and the setup_activity_v2
that previously sat behind the integration marker.

WorkflowV2 is exercised by stubbing `workflow.execute_activity` so we
never need a live Temporal worker — we just verify the workflow's
control flow (skip / resume, fan-out, failure → compensation, etc.).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# setup_activity_v2 — direct invocation
# ---------------------------------------------------------------------------


def test_setup_activity_v2_progress_path() -> None:
    from agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_provisioning_team.temporal import activities as t_acts

    fake_setup_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1"),
    )
    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    recorded = []

    def fake_safe(fn_name, *args, **kwargs):
        recorded.append({"fn": fn_name, "args": args, "kwargs": kwargs})

    with (
        patch.object(t_acts, "_safe", side_effect=fake_safe),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_provisioning_team.phases.setup.run_setup",
            return_value=fake_setup_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity_v2("j", "a", "default.yaml")

    assert payload["success"] is True
    assert payload["environment"]["container_id"] == "c1"
    # mark_job_running + update_job were invoked.
    fn_names = [r["fn"] for r in recorded]
    assert "mark_job_running" in fn_names
    assert "update_job" in fn_names
    assert "add_completed_phase" in fn_names


def test_setup_activity_v2_raises_when_setup_fails() -> None:
    from agent_provisioning_team.models import SetupResult
    from agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    with (
        patch.object(t_acts, "_safe"),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_provisioning_team.phases.setup.run_setup",
            return_value=SetupResult(success=False, error="setup boom"),
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="setup boom"):
            t_acts.setup_activity_v2("j", "a", "default.yaml")


def test_setup_activity_v2_restores_from_prior() -> None:
    from agent_provisioning_team.temporal import activities as t_acts

    prior = {
        "success": True,
        "environment": {
            "container_id": "c1",
            "container_name": "c1",
            "workspace_path": "/w",
            "status": "running",
        },
    }
    with (
        patch.object(t_acts, "_safe"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity_v2("j", "a", "default.yaml", prior_setup=prior)
    assert payload["success"] is True
    assert payload["environment"]["container_id"] == "c1"


# ---------------------------------------------------------------------------
# AgentProvisioningWorkflowV2 — direct .run() invocation
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
async def test_workflow_v2_happy_path(tmp_path, monkeypatch) -> None:
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity_v2": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity_v2": {
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
            "audit_activity_v2": {"passed": True, "verifications": []},
            "documentation_activity_v2": {"success": True, "onboarding": {"summary": "s"}},
            "deliver_activity_v2": {"success": True, "error": None},
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        workflow = wf.AgentProvisioningWorkflowV2()
        await workflow.run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "setup_activity_v2" in fn_names
    assert "credentials_activity_v2" in fn_names
    # Two tools → two provision activities.
    assert fn_names.count("provision_tool_activity") == 2
    assert "audit_activity_v2" in fn_names
    assert "documentation_activity_v2" in fn_names
    assert "deliver_activity_v2" in fn_names


@pytest.mark.asyncio
async def test_workflow_v2_compensates_on_tool_failure(tmp_path) -> None:
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
            "setup_activity_v2": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity_v2": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity_v2": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Tool provisioning failed"):
            await wf.AgentProvisioningWorkflowV2().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    # Compensate was invoked
    assert "compensate_activity_v2" in fn_names


@pytest.mark.asyncio
async def test_workflow_v2_skips_provisioning_when_resumed(tmp_path) -> None:
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity_v2": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity_v2": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "audit_activity_v2": {"passed": True, "verifications": []},
            "documentation_activity_v2": {"success": True, "onboarding": {"summary": "s"}},
            "deliver_activity_v2": {"success": True, "error": None},
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
        await wf.AgentProvisioningWorkflowV2().run(
            "job-1",
            "agent-1",
            manifest_path,
            skip_phases=["account_provisioning"],
            prior_results=prior,
        )

    fn_names = [c["name"] for c in stub.calls]
    # No per-tool provisioning happened
    assert "provision_tool_activity" not in fn_names
    # Compensation is skipped because everything in prior was successful
    assert "compensate_activity_v2" not in fn_names


@pytest.mark.asyncio
async def test_workflow_v2_resume_with_prior_failed_tools_compensates(tmp_path) -> None:
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity_v2": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity_v2": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "compensate_activity_v2": None,
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
            await wf.AgentProvisioningWorkflowV2().run(
                "job-1",
                "agent-1",
                manifest_path,
                skip_phases=["account_provisioning"],
                prior_results=prior,
            )

    fn_names = [c["name"] for c in stub.calls]
    assert "compensate_activity_v2" in fn_names


@pytest.mark.asyncio
async def test_workflow_v1_invokes_single_activity(tmp_path) -> None:
    """v1 workflow simply delegates to ``run_provisioning_activity``."""
    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub({"run_provisioning_activity": None})
    with patch.object(wf.workflow, "execute_activity", new=stub):
        await wf.AgentProvisioningWorkflow().run("j", "a", "default.yaml")

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == ["run_provisioning_activity"]


@pytest.mark.asyncio
async def test_workflow_v2_handles_non_dict_provision_results(tmp_path) -> None:
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
            "setup_activity_v2": {"success": True, "environment": None},
            "credentials_activity_v2": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity_v2": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="Tool provisioning failed"):
            await wf.AgentProvisioningWorkflowV2().run("job-1", "agent-1", manifest_path)


@pytest.mark.asyncio
async def test_workflow_v2_handles_dict_failure_results(tmp_path) -> None:
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
            "setup_activity_v2": {"success": True, "environment": None},
            "credentials_activity_v2": {
                "success": True,
                "credentials": {
                    "postgresql": {"tool_name": "postgresql"},
                    "redis": {"tool_name": "redis"},
                },
            },
            "provision_tool_activity": provision_responder,
            "compensate_activity_v2": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="redis down"):
            await wf.AgentProvisioningWorkflowV2().run("job-1", "agent-1", manifest_path)
