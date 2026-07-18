"""Unit tests for AgentProvisioningWorkflow.

The workflow is exercised by stubbing `workflow.execute_activity` so we
never need a live Temporal worker — we just verify the workflow's
control flow (skip / resume, fan-out, failure → compensation, etc.).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patched_true(monkeypatch):
    """Default every test to the post-lock-deploy replay branch.

    ``workflow.patched(...)`` needs a real workflow event loop; direct
    ``.run()`` calls here have none, so it must be stubbed like
    ``execute_activity``/``info``. ``True`` matches a fresh (non-replayed)
    execution — what nearly every test in this module wants — mirroring
    ``market_research_team``'s ``test_temporal_workflow.py`` idiom. Tests
    exercising the pre-lock replay path override this locally via the same
    ``monkeypatch`` fixture (last ``setattr`` wins).
    """
    from agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: True)


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
                "credentials": {
                    "tool_name": call["args"][2],
                    "connection_string": f"conn-{call['args'][2]}",
                },
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
    assert (
        _call(stub, "documentation_activity")["args"][3]["postgresql"]["connection_string"]
        == "conn-postgresql"
    )
    assert _call(stub, "deliver_activity")["args"][3]["redis"]["connection_string"] == "conn-redis"
    # The per-agent_id lock (issue #1489) is acquired before setup and
    # released after everything else, regardless of what ran in between.
    assert fn_names[0] == "acquire_agent_lock_activity"
    assert fn_names[-1] == "release_agent_lock_activity"
    assert _call(stub, "acquire_agent_lock_activity")["args"] == ["job-1", "agent-1"]
    assert _call(stub, "release_agent_lock_activity")["args"] == ["job-1", "agent-1"]
    # The lease is renewed between every scheduled activity (P1 regression:
    # a long-running job must never lose its own lock to LOCK_TTL_S expiry,
    # and no un-renewed gap may exceed the tool fan-out's own worst case) —
    # one initial acquire plus one renewal after each of the pre-existing-
    # environment check / setup / list_manifest_tools / credentials / tool
    # fan-out / account_provisioning checkpoint / audit / documentation = 9
    # total acquire_agent_lock_activity calls, each renewing for the same
    # owner.
    acquire_calls = [c for c in stub.calls if c["name"] == "acquire_agent_lock_activity"]
    assert len(acquire_calls) == 9
    assert all(c["args"] == ["job-1", "agent-1"] for c in acquire_calls)
    # No two consecutive non-lock activities ever run back-to-back without a
    # renewal between them (P1 regression: a gap spanning two un-renewed
    # activities — e.g. list_manifest_tools + credentials — can exceed even
    # a generously configured TTL). provision_tool_activity's own parallel
    # fan-out (several calls with no renewal *between* them, by design —
    # asyncio.gather) is collapsed to one slot before checking.
    lock_names = {"acquire_agent_lock_activity", "release_agent_lock_activity"}
    collapsed = []
    for name in fn_names:
        if (
            name == "provision_tool_activity"
            and collapsed
            and collapsed[-1] == "provision_tool_activity"
        ):
            continue
        collapsed.append(name)
    for prev, nxt in zip(collapsed, collapsed[1:]):
        assert prev in lock_names or nxt in lock_names, (
            f"no lock renewal between consecutive activities {prev!r} -> {nxt!r}: {fn_names}"
        )


@pytest.mark.asyncio
async def test_workflow_unpatched_replay_skips_lock_activities(tmp_path, monkeypatch) -> None:
    """P1 regression: a history recorded before the lock existed
    (workflow.patched -> False) must replay its original lock-free command
    sequence exactly, or Temporal reports nondeterminism and strands the
    in-flight execution. No acquire/renew/release activity is scheduled."""
    from agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: False)
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
        await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "acquire_agent_lock_activity" not in fn_names
    assert "release_agent_lock_activity" not in fn_names
    assert fn_names[0] == "setup_activity"
    assert fn_names[-1] == "deliver_activity"


@pytest.mark.asyncio
async def test_workflow_post_lock_pre_check_replay_skips_new_activity_and_renewal(
    tmp_path, monkeypatch
) -> None:
    """P1 regression: a history recorded after the lock existed but before
    check_existing_environment_activity was introduced must replay without
    that activity or its accompanying renewal call. Both are new commands
    relative to that history's already-recorded "lock acquired -> setup"
    sequence — reusing _PROVISIONING_LOCK_PATCH (already True for such a
    history) to gate them would insert unrecorded commands into its replay
    and report Temporal nondeterminism. A dedicated, independent
    _PRE_EXISTING_ENV_CHECK_PATCH marker is required.
    """
    from agent_provisioning_team.temporal import workflows as wf

    def _patched(marker, *a, **k):
        return marker != wf._PRE_EXISTING_ENV_CHECK_PATCH

    monkeypatch.setattr(wf.workflow, "patched", _patched)
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
        await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "check_existing_environment_activity" not in fn_names
    # Exactly the pre-this-round sequence: acquire, then straight to setup —
    # no extra renewal call inserted for the skipped check.
    assert fn_names[0] == "acquire_agent_lock_activity"
    assert fn_names[1] == "setup_activity"


@pytest.mark.asyncio
async def test_workflow_releases_lock_when_deliver_fails(tmp_path) -> None:
    """The agent_id lock is released even when the workflow ultimately raises."""
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
            "deliver_activity": RuntimeError("deliver boom"),
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="deliver boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names[0] == "acquire_agent_lock_activity"
    assert fn_names[-1] == "release_agent_lock_activity"
    assert _call(stub, "release_agent_lock_activity")["args"] == ["job-1", "agent-1"]


@pytest.mark.asyncio
async def test_workflow_releases_lock_when_acquire_itself_fails(tmp_path) -> None:
    """A failed lock acquire (exhausted retries) still marks the job failed and
    releases (a safe no-op — this job never held the lock)."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "acquire_agent_lock_activity": RuntimeError(
                "agent 'agent-1' is currently locked by owner 'job-0'"
            ),
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="currently locked"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == [
        "acquire_agent_lock_activity",
        "mark_job_failed_activity",
        "release_agent_lock_activity",
    ]
    # This run never held the lock at all, so compensating (which would be
    # keyed on agent_id alone, like every teardown path) could tear down
    # whatever job currently does hold it — must not be attempted.
    assert "compensate_activity" not in fn_names


@pytest.mark.asyncio
async def test_workflow_skips_compensation_when_renewal_loses_the_lock(tmp_path) -> None:
    """P1 regression: if a lock renewal fails after setup (the agent_id lock
    was reclaimed by a replacement job, or any other renewal error), the
    except block must NOT run unfenced by-agent_id compensation — that would
    tear down the replacement job's live resources, recreating the exact
    cross-job teardown race this lock exists to prevent. A lost lock still
    marks the job failed and (harmlessly, since we no longer own it) attempts
    release."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    acquire_calls = {"n": 0}

    def _acquire_side_effect(call):
        acquire_calls["n"] += 1
        if acquire_calls["n"] >= 3:  # 1=initial acquire, 2=renewal after setup
            raise RuntimeError("agent 'agent-1' is currently locked by owner 'job-2'")
        return None

    stub = _ExecActivityStub(
        {
            "acquire_agent_lock_activity": _acquire_side_effect,
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="currently locked"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "compensate_activity" not in fn_names
    assert "mark_job_failed_activity" in fn_names
    assert fn_names[-1] == "release_agent_lock_activity"


@pytest.mark.asyncio
async def test_workflow_original_error_survives_a_failed_release(tmp_path) -> None:
    """A release_agent_lock_activity failure is logged, not raised — the
    original failure it's cleaning up after must still propagate unmasked."""
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)

    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "list_manifest_tools_activity": _TOOL_SPECS,
            "credentials_activity": RuntimeError("credentials boom"),
            "release_agent_lock_activity": RuntimeError("release also boom"),
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="credentials boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names[-1] == "release_agent_lock_activity"


def test_merge_enriched_credentials_no_credentials_key() -> None:
    """A successful tool result without a `credentials` key leaves the base entry unchanged."""
    from agent_provisioning_team.temporal import workflows as wf

    credentials_by_tool = {
        "postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"}
    }
    tool_results_dump = [{"tool_name": "postgresql", "success": True, "provisioner_key": "x"}]

    merged = wf.AgentProvisioningWorkflow._merge_enriched_credentials(
        credentials_by_tool, tool_results_dump
    )

    assert merged == {"postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"}}


def test_merge_enriched_credentials_tool_not_in_base_map() -> None:
    """A successful result for a tool absent from the base map adds a new entry, leaving others untouched."""
    from agent_provisioning_team.temporal import workflows as wf

    credentials_by_tool = {
        "postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"}
    }
    tool_results_dump = [
        {
            "tool_name": "redis",
            "success": True,
            "credentials": {"tool_name": "redis", "connection_string": "conn-redis"},
        }
    ]

    merged = wf.AgentProvisioningWorkflow._merge_enriched_credentials(
        credentials_by_tool, tool_results_dump
    )

    assert merged["postgresql"] == {"tool_name": "postgresql", "username": "u", "password": "p"}
    assert merged["redis"] == {"tool_name": "redis", "connection_string": "conn-redis"}


def test_merge_enriched_credentials_non_dict_tool_result() -> None:
    """Non-dict entries in tool_results_dump are skipped without raising."""
    from agent_provisioning_team.temporal import workflows as wf

    credentials_by_tool = {
        "postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"}
    }
    tool_results_dump = ["not-a-dict", None, 42]

    merged = wf.AgentProvisioningWorkflow._merge_enriched_credentials(
        credentials_by_tool, tool_results_dump
    )

    assert merged == {"postgresql": {"tool_name": "postgresql", "username": "u", "password": "p"}}


def test_merge_enriched_credentials_does_not_mutate_input() -> None:
    """The method returns a new mapping and leaves the caller's dicts untouched."""
    from agent_provisioning_team.temporal import workflows as wf

    original_entry = {"tool_name": "postgresql", "username": "u", "password": "p"}
    credentials_by_tool = {"postgresql": original_entry}
    tool_results_dump = [
        {
            "tool_name": "postgresql",
            "success": True,
            "credentials": {"connection_string": "conn-postgresql"},
        }
    ]

    merged = wf.AgentProvisioningWorkflow._merge_enriched_credentials(
        credentials_by_tool, tool_results_dump
    )

    assert credentials_by_tool == {"postgresql": original_entry}
    assert "connection_string" not in original_entry
    assert merged["postgresql"]["connection_string"] == "conn-postgresql"
    assert merged is not credentials_by_tool


@pytest.mark.asyncio
async def test_compensate_failed_tools_clears_reused_when_tearing_down_environment() -> None:
    """A tool marked reused must not be trusted when nothing predates this run.

    ``reused=True`` on a tool result can also mean Temporal retried
    ``provision_tool_activity`` after its response was lost — the retry's
    idempotent create then reads back THIS run's own first-attempt write as
    "existing". When ``tear_down_environment=True`` (no environment predates
    this run, so there is nothing else ``reused`` could refer to), that
    apparent reuse must be overridden to False before compensating, or it
    would wrongly exclude the tool from rollback and leak it.
    """
    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub({"compensate_activity": None})
    succeeded = [
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": True},
    ]

    with patch.object(wf.workflow, "execute_activity", new=stub):
        await wf.AgentProvisioningWorkflow()._compensate_failed_tools(
            "agent-1", succeeded, "job-1", tear_down_environment=True
        )

    call = _call(stub, "compensate_activity")
    assert call["args"] == [
        "agent-1",
        [{"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": False}],
        "job-1",
        True,
    ]


@pytest.mark.asyncio
async def test_compensate_failed_tools_preserves_reused_when_environment_predates_run() -> None:
    """A tool marked reused is passed through unmodified when an environment does predate this run.

    ``tear_down_environment=False`` means ``pre_existing_environment`` was
    True — a reused account there really can predate this run (e.g. a re-run
    against an already-delivered agent), so nothing overrides it here.
    """
    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub({"compensate_activity": None})
    succeeded = [
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": True},
    ]

    with patch.object(wf.workflow, "execute_activity", new=stub):
        await wf.AgentProvisioningWorkflow()._compensate_failed_tools(
            "agent-1", succeeded, "job-1", tear_down_environment=False
        )

    call = _call(stub, "compensate_activity")
    assert call["args"] == [
        "agent-1",
        [{"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": True}],
        "job-1",
        False,
    ]


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
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": False}
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
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": False}
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
        {"tool_name": "postgresql", "provisioner_key": "postgres_provisioner", "reused": False}
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
        {"tool_name": "postgresql", "provisioner_key": "x", "reused": False}
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
        {"tool_name": "postgresql", "provisioner_key": "x", "reused": False}
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
    assert compensate_call["args"] == ["agent-1", [], "job-1", True]
    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]


@pytest.mark.asyncio
async def test_workflow_setup_reused_false_overrides_conservative_pre_check(tmp_path) -> None:
    """Setup's own confirmed-fresh outcome corrects an inconclusive pre-check.

    check_existing_environment_activity's pre-check can be conservative (an
    unreadable registry, or now, a stale record whose container turned out
    to be gone) and report pre_existing_environment=True even though
    run_setup then goes on to create an entirely fresh container. Since a
    container run_setup just created cannot also predate this run, its own
    environment.reused=False must override that earlier guess — a later
    failure must still tear the fresh environment down (tear_down_environment
    stays True), not preserve it as if it were pre-existing.
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "check_existing_environment_activity": True,
            "setup_activity": {
                "success": True,
                "environment": {"workspace_path": "/w", "reused": False},
            },
            "credentials_activity": RuntimeError("cred boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="cred boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1", True]


@pytest.mark.asyncio
async def test_workflow_setup_reused_true_does_not_override_pre_check(tmp_path) -> None:
    """environment.reused=True must never flip pre_existing_environment to True.

    Unlike reused=False (unambiguous), reused=True is not trustworthy
    evidence of a genuinely pre-existing environment on its own — it can
    also reflect this same run's own retried setup_activity reading back its
    own earlier (response-lost) success as "already there". So it must never
    override a pre-check that already concluded pre_existing_environment is
    False (tear_down_environment stays True here, unaffected by
    environment.reused).
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "check_existing_environment_activity": False,
            "setup_activity": {
                "success": True,
                "environment": {"workspace_path": "/w", "reused": True},
            },
            "credentials_activity": RuntimeError("cred boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="cred boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1", True]


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
async def test_workflow_setup_failure_compensates_and_marks_failed(tmp_path) -> None:
    """A setup failure gets both ``run_setup``'s local rollback AND workflow compensation.

    ``run_setup`` already ran its own local best-effort rollback (scoped to
    resources that attempt created) before this exception ever reached the
    workflow. This run holds ``agent_id``'s exclusive lock for its entire
    duration and ``agent_id`` had no environment before this run started, so
    workflow-level ``compensate([])`` is safe to run here too, as a second,
    independently-retried backstop for when the local rollback itself fails
    (e.g. a transient ``docker rm`` error) — it cannot tear down a healthy
    environment another job owns, since no other job can be running against
    this ``agent_id`` while the lock is held, and there was nothing
    pre-existing for it to destroy either.
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "check_existing_environment_activity": False,
            "setup_activity": RuntimeError("setup boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with patch.object(wf.workflow, "execute_activity", new=stub):
        with pytest.raises(RuntimeError, match="setup boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1", True]
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "setup boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_skips_environment_teardown_when_environment_pre_existed(tmp_path) -> None:
    """A setup failure must not tear down an environment that pre-dated this run.

    Holding the lock only rules out a CONCURRENT workflow — it says nothing
    about whether THIS run created what's currently at agent_id. If
    check_existing_environment_activity reports agent_id already had a
    running environment before this run touched anything (e.g. setup's
    already-running fast path reused it and created nothing, then a later
    checkpoint write failed), compensation must still run — to roll back any
    tool-level side effects this run created — but must pass
    ``tear_down_environment=False`` so it does not destroy the Docker
    env/credential store/environment record that predates this run.
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "check_existing_environment_activity": True,
            "setup_activity": RuntimeError("checkpoint boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="checkpoint boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1", False]
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "checkpoint boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_unpatched_replay_still_compensates_on_setup_failure(
    tmp_path, monkeypatch
) -> None:
    """A pre-lock-deploy replay must still compensate on a setup failure.

    ``_acquire_agent_lock`` returns False (a no-op) when replaying a history
    from before the lock existed — but before the lock existed at all, this
    except block's only gate was the progress flags
    (``account_provisioning_done`` / ``tools_phase_compensated``), so such a
    history may already contain a recorded ``compensate_activity`` command
    for a setup failure. Gating solely on ``lock_acquired`` would make that
    decision False for EVERY pre-lock replay (a pre-lock no-op never sets it
    True), silently dropping that recorded command — a regression test for
    exactly that: the guard must fall back to the pre-lock shape (ignore the
    lock check entirely) whenever this is a pre-lock replay specifically,
    rather than conflating "never held the lock because this predates it"
    with "never held the lock because acquiring it just failed".
    """
    from agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: False)
    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": RuntimeError("setup boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": None,
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="setup boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    fn_names = [c["name"] for c in stub.calls]
    assert "acquire_agent_lock_activity" not in fn_names
    assert "check_existing_environment_activity" not in fn_names
    compensate_call = _call(stub, "compensate_activity")
    assert compensate_call["args"] == ["agent-1", [], "job-1", False]


@pytest.mark.asyncio
async def test_workflow_credentials_failure_compensation_raises(tmp_path) -> None:
    """A failing compensation must be logged, not mask the original error.

    After setup succeeds, a credentials failure triggers ``compensate([])`` to
    tear down the Docker env. The except-block wraps that in a nested try/except:
    if ``compensate_activity`` raises, it is logged via ``workflow.logger.error``
    and the original credentials exception still propagates. The job is still
    marked failed afterwards.
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity": RuntimeError("cred boom"),
            "compensate_activity": RuntimeError("compensate boom"),
            "mark_job_failed_activity": None,
        }
    )

    # `workflow.logger.error` raises outside a workflow event loop, so patch the
    # logger to a mock — this both keeps the harness happy and lets us assert the
    # compensation-failure branch was taken.
    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger") as mock_logger,
    ):
        with pytest.raises(RuntimeError, match="cred boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    # Compensation was attempted (and raised), the failure was logged, and the
    # job was still marked failed despite the compensation error.
    assert _call(stub, "compensate_activity")["args"] == ["agent-1", [], "job-1", True]
    mock_logger.error.assert_called_once()
    fail_call = _call(stub, "mark_job_failed_activity")
    assert fail_call["args"][0] == "job-1"
    assert "cred boom" in fail_call["args"][1]


@pytest.mark.asyncio
async def test_workflow_credentials_failure_mark_failed_raises(tmp_path) -> None:
    """A failing mark_job_failed must be logged, not mask the original error.

    After setup succeeds, a credentials failure triggers compensation (which
    succeeds here) and then the terminal ``mark_job_failed_activity`` write. The
    except-block wraps that write in a nested try/except: if it raises, it is
    logged via ``workflow.logger.error`` and the original credentials exception
    still propagates.
    """
    from agent_provisioning_team.temporal import workflows as wf

    manifest_path = _build_manifest_yaml(tmp_path)
    stub = _ExecActivityStub(
        {
            "setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
            "credentials_activity": RuntimeError("cred boom"),
            "compensate_activity": None,
            "mark_job_failed_activity": RuntimeError("mark boom"),
        }
    )

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "logger") as mock_logger,
    ):
        with pytest.raises(RuntimeError, match="cred boom"):
            await wf.AgentProvisioningWorkflow().run("job-1", "agent-1", manifest_path)

    # Compensation succeeded (no log); the mark_job_failed failure was logged
    # exactly once and did not mask the original credentials exception.
    assert "mark_job_failed_activity" in [c["name"] for c in stub.calls]
    mock_logger.error.assert_called_once()


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


# ---------------------------------------------------------------------------
# AgentDeprovisioningWorkflow — direct .run() invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprovisioning_workflow_calls_deprovision_activity() -> None:
    """run() dispatches deprovision_activity with (agent_id, force) and returns its result,
    after acquiring the agent_id lock (using its own workflow id as owner) and releasing it after."""
    from types import SimpleNamespace

    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub(
        {
            "deprovision_activity": {
                "agent_id": "agent-1",
                "success": True,
                "details": {"tools": {"postgresql": True}},
                "error": None,
            },
        }
    )
    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-agent-1-abc123")

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "info", return_value=fake_info),
    ):
        result = await wf.AgentDeprovisioningWorkflow().run("agent-1", True)

    assert result == {
        "agent_id": "agent-1",
        "success": True,
        "details": {"tools": {"postgresql": True}},
        "error": None,
    }
    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == [
        "acquire_agent_lock_activity",
        "deprovision_activity",
        "release_agent_lock_activity",
    ]
    owner = fake_info.workflow_id
    assert _call(stub, "acquire_agent_lock_activity")["args"] == [owner, "agent-1"]
    assert _call(stub, "release_agent_lock_activity")["args"] == [owner, "agent-1"]
    assert _call(stub, "deprovision_activity")["args"] == ["agent-1", True]


@pytest.mark.asyncio
async def test_deprovisioning_workflow_unpatched_replay_skips_lock_activities(monkeypatch) -> None:
    """P1 regression: same replay-safety requirement as
    test_workflow_unpatched_replay_skips_lock_activities, for the
    deprovisioning workflow."""
    from types import SimpleNamespace

    from agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: False)
    stub = _ExecActivityStub(
        {
            "deprovision_activity": {
                "agent_id": "agent-1",
                "success": True,
                "details": {},
                "error": None,
            },
        }
    )
    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-agent-1-legacy")

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "info", return_value=fake_info),
    ):
        result = await wf.AgentDeprovisioningWorkflow().run("agent-1", False)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == ["deprovision_activity"]
    assert result["success"] is True


@pytest.mark.asyncio
async def test_deprovisioning_workflow_releases_lock_when_deprovision_fails() -> None:
    """The agent_id lock is released even when deprovision_activity raises."""
    from types import SimpleNamespace

    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub({"deprovision_activity": RuntimeError("deprovision boom")})
    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-agent-1-def456")

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "info", return_value=fake_info),
    ):
        with pytest.raises(RuntimeError, match="deprovision boom"):
            await wf.AgentDeprovisioningWorkflow().run("agent-1", False)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == [
        "acquire_agent_lock_activity",
        "deprovision_activity",
        "release_agent_lock_activity",
    ]


@pytest.mark.asyncio
async def test_deprovisioning_workflow_original_error_survives_a_failed_release() -> None:
    """A release_agent_lock_activity failure is logged, not raised — the
    original deprovision_activity failure must still propagate unmasked."""
    from types import SimpleNamespace

    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub(
        {
            "deprovision_activity": RuntimeError("deprovision boom"),
            "release_agent_lock_activity": RuntimeError("release also boom"),
        }
    )
    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-agent-1-ghi789")

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "info", return_value=fake_info),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="deprovision boom"):
            await wf.AgentDeprovisioningWorkflow().run("agent-1", False)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names[-1] == "release_agent_lock_activity"


@pytest.mark.asyncio
async def test_deprovisioning_workflow_releases_when_acquire_itself_fails() -> None:
    """P2 regression: acquire lives inside the try/finally, so even when the
    acquire activity call fails/times out client-side, release is still
    attempted — Temporal activities are at-least-once, so the acquire's side
    effect may have persisted server-side despite the client-visible failure;
    without this, that successful-but-unobserved acquire would orphan the
    lock until LOCK_TTL_S. release() itself is a safe no-op if the acquire
    genuinely never wrote a record."""
    from types import SimpleNamespace

    from agent_provisioning_team.temporal import workflows as wf

    stub = _ExecActivityStub(
        {
            "acquire_agent_lock_activity": RuntimeError("acquire timed out"),
        }
    )
    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-agent-1-jkl012")

    with (
        patch.object(wf.workflow, "execute_activity", new=stub),
        patch.object(wf.workflow, "info", return_value=fake_info),
    ):
        with pytest.raises(RuntimeError, match="acquire timed out"):
            await wf.AgentDeprovisioningWorkflow().run("agent-1", False)

    fn_names = [c["name"] for c in stub.calls]
    assert fn_names == [
        "acquire_agent_lock_activity",
        "release_agent_lock_activity",
    ]
    assert "deprovision_activity" not in fn_names
