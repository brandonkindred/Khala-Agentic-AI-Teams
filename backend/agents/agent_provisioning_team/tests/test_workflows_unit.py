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


_TOOL_SPECS = [
    {"name": "postgresql", "provisioner": "postgres_provisioner", "config": {}},
    {"name": "redis", "provisioner": "redis_provisioner", "config": {}},
]


def _call(stub: _ExecActivityStub, name: str) -> dict:
    return next(c for c in stub.calls if c["name"] == name)


@pytest.mark.asyncio
async def test_workflow_happy_path(tmp_path, monkeypatch) -> None:
    """Happy path runs setup → credentials → per-tool provision → audit → docs → deliver."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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
    assert "list_manifest_tools_activity" in fn_names
    creds_call = _call(stub, "credentials_activity")
    assert creds_call["args"][4] == _TOOL_SPECS
    assert "credentials_activity" in fn_names
    provision_calls = [c for c in stub.calls if c["name"] == "provision_tool_activity"]
    assert [c["args"][2] for c in provision_calls] == ["postgresql", "redis"]
    assert "record_account_provisioning_activity" in fn_names
    assert "audit_activity" in fn_names
    assert "documentation_activity" in fn_names
    assert "deliver_activity" in fn_names


@pytest.mark.asyncio
async def test_workflow_compensates_on_tool_failure(tmp_path) -> None:
    """When a tool fails, succeeded tools are compensated and the job is marked failed."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

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
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"][0] == "agent-1"
    assert compensate_call["args"][1] == [
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner"}
    ]
    assert [c["name"] for c in stub.calls].count("mark_job_failed_activity") == 1


@pytest.mark.asyncio
async def test_workflow_skips_provisioning_when_resumed(tmp_path) -> None:
    """Resume with prior successful account_provisioning skips per-tool fan-out."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    prior_tools = [
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
    prior = {"account_provisioning": {"tool_results": prior_tools}}

    with patch.object(wf.workflow, "execute_activity", new=stub):
        await wf.AgentProvisioningWorkflow().run(
            "job-1",
            "agent-1",
            manifest_path,
            skip_phases=["account_provisioning"],
            prior_results=prior,
        )

    fn_names = [c["name"] for c in stub.calls]
    assert "provision_tool_activity" not in fn_names
    assert "record_account_provisioning_activity" not in fn_names
    assert "compensate_activity" not in fn_names

    assert _call(stub, "audit_activity")["args"][3] == prior_tools
    assert _call(stub, "documentation_activity")["args"][4] == prior_tools
    assert _call(stub, "deliver_activity")["args"][4] == prior_tools


@pytest.mark.asyncio
async def test_workflow_resume_tool_set_mismatch_compensates_prior_successes(tmp_path) -> None:
    """Mismatch after restore fails the job but rolls back prior successful tools."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"][0] == "agent-1"
    assert compensate_call["args"][1] == [
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner"}
    ]
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "Cannot restore account_provisioning" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_resume_with_prior_failed_tools_compensates(tmp_path) -> None:
    """Resume restoring a prior phase that includes failed tools still compensates."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"][1] == [
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner"}
    ]
    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]


@pytest.mark.asyncio
async def test_workflow_handles_non_dict_provision_results(tmp_path) -> None:
    """A provision_tool_activity result that isn't a dict (e.g. None) → failure path."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    def provision_responder(call):
        tool_name = call["args"][2]
        if tool_name == "redis":
            return None
        return {"tool_name": "postgresql", "success": True, "provisioner_key": "x"}

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": None},
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    assert _call(stub, "compensate_activity")["args"][1] == [
        {"tool_name": "postgresql", "provisioner_key": "x"}
    ]


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
            "list_manifest_tools_activity": _TOOL_SPECS,
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

    assert _call(stub, "compensate_activity")["args"][1] == [
        {"tool_name": "postgresql", "provisioner_key": "x"}
    ]


@pytest.mark.asyncio
async def test_workflow_marks_failed_on_audit_error(tmp_path) -> None:
    """Non-tool phase exceptions must persist terminal failure before re-raising."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "audit boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_compensates_setup_on_credentials_failure(tmp_path) -> None:
    """After setup succeeds, credential failure must compensate (tear down env)."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity": RuntimeError("cred boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="cred boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1"]
    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]


@pytest.mark.asyncio
async def test_workflow_compensates_succeeded_tools_on_checkpoint_failure(tmp_path) -> None:
    """Checkpoint failure after fan-out must roll back tools that already succeeded."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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
                "provisioner_key": f"{call['args'][2]}_provisioner",
            },
            "record_account_provisioning_activity": RuntimeError("checkpoint boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="checkpoint boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"][0] == "agent-1"
    assert {t["tool_name"] for t in compensate_call["args"][1]} == {"postgresql", "redis"}
    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]


@pytest.mark.asyncio
async def test_workflow_setup_failure_marks_failed_without_compensate(tmp_path) -> None:
    """Setup failure has nothing to roll back — mark failed, skip compensate."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": RuntimeError("setup boom"),
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="setup boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "compensate_activity" not in fn_names
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "setup boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_marks_failed_on_documentation_error(tmp_path) -> None:
    """Documentation failure persists terminal failure before re-raising."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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
            "audit_activity": {"passed": True, "verifications": []},
            "documentation_activity": RuntimeError("docs boom"),
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="docs boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "docs boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_marks_failed_on_deliver_error(tmp_path) -> None:
    """Deliver failure persists terminal failure before re-raising."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
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
            "audit_activity": {"passed": True, "verifications": []},
            "documentation_activity": {"success": True, "onboarding": {"summary": "s"}},
            "deliver_activity": RuntimeError("deliver boom"),
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="deliver boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "deliver boom" in fail_call["args"][1]
