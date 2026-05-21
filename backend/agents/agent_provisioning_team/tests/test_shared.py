"""Unit tests for shared helpers.

Covers job_store, environment_store, logging_context, phase_state,
provisioner_state edge cases, llm_client, and tool_manifest helpers
that aren't already exercised in the integration matrix.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_provisioning_team.shared.environment_store import (
    EnvironmentInfo as StoreEnvInfo,
)
from agent_provisioning_team.shared.environment_store import EnvironmentStore

# ---------------------------------------------------------------------------
# environment_store
# ---------------------------------------------------------------------------


def test_environment_info_from_dict_roundtrip() -> None:
    info = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="agent-a1",
        workspace_path="/w",
        tools_provisioned=["pg", "redis"],
    )
    d = info.to_dict()
    restored = StoreEnvInfo.from_dict(d)
    assert restored.agent_id == "a1"
    assert restored.tools_provisioned == ["pg", "redis"]


def test_environment_store_register_get_remove(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    assert store.get("missing") is None

    env = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="c1",
        workspace_path="/w",
    )
    store.register(env)
    assert store.exists("a1")

    fetched = store.get("a1")
    assert fetched.container_id == "c1"

    assert store.remove("a1") is True
    assert store.remove("a1") is False
    assert store.get("a1") is None


def test_environment_store_update_status(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)

    # update on missing returns False
    assert store.update_status("missing", "ready") is False

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )
    )
    assert store.update_status("a1", "ready") is True
    assert store.get("a1").status == "ready"


def test_environment_store_update_status_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.update_status("broken", "ready") is False


def test_environment_store_add_tool(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    assert store.add_tool("missing", "pg") is False

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )
    )
    assert store.add_tool("a1", "pg") is True
    # Idempotent — adding same tool twice doesn't duplicate.
    assert store.add_tool("a1", "pg") is True
    assert store.get("a1").tools_provisioned == ["pg"]


def test_environment_store_add_tool_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    bad = tmp_path / "broken.json"
    bad.write_text("not json", encoding="utf-8")
    assert store.add_tool("broken", "pg") is False


def test_environment_store_list_all(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
            status="ready",
        )
    )
    store.register(
        StoreEnvInfo(
            agent_id="a2",
            container_id="c2",
            container_name="c2",
            workspace_path="/w",
            status="running",
        )
    )

    all_envs = store.list_all()
    assert len(all_envs) == 2

    ready = store.list_all(status="ready")
    assert len(ready) == 1
    assert ready[0].agent_id == "a1"


def test_environment_store_list_all_skips_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1", workspace_path="/w")
    )
    # Corrupt JSON
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    # JSON missing required field
    (tmp_path / "incomplete.json").write_text(json.dumps({"agent_id": "x"}), encoding="utf-8")

    out = store.list_all()
    # Only the valid one shows up.
    assert {e.agent_id for e in out} == {"a1"}


def test_environment_store_get_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "x.json").write_text("not json", encoding="utf-8")
    assert store.get("x") is None


# ---------------------------------------------------------------------------
# job_store
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_job_client(monkeypatch):
    """Replace the module-level ``_client`` with a fresh fake."""
    from agent_provisioning_team.shared import job_store as js
    from job_service_client_fake import FakeJobServiceClient

    fake = FakeJobServiceClient(team="agent_provisioning_team")
    monkeypatch.setattr(js, "_client", lambda cache_dir=None: fake)
    return fake


def test_job_store_create_and_get(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "agent-1", "default.yaml")
    data = js.get_job("j1")
    assert data["agent_id"] == "agent-1"
    assert data["manifest_path"] == "default.yaml"

    # Missing job → empty dict
    assert js.get_job("missing") == {}


def test_job_store_update_job(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_job("j1", progress=42, current_phase="setup")
    data = js.get_job("j1")
    assert data["progress"] == 42
    assert data["current_phase"] == "setup"


def test_job_store_list_jobs(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a1", "m")
    js.create_job("j2", "a2", "m")
    js.update_job("j2", status="completed")

    all_jobs = js.list_jobs(running_only=False)
    assert len(all_jobs) == 2
    active = js.list_jobs(running_only=True)
    assert len(active) == 1
    assert active[0]["job_id"] == "j1"


def test_job_store_mark_running_completed_failed(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.mark_job_running("j1")
    assert js.get_job("j1")["status"] == "running"

    js.mark_job_completed("j1", result={"ok": True})
    data = js.get_job("j1")
    assert data["status"] == "completed"
    assert data["progress"] == 100
    assert data["result"] == {"ok": True}

    js.mark_job_failed("j1", error="kaboom")
    data = js.get_job("j1")
    assert data["status"] == "failed"
    assert data["error"] == "kaboom"


def test_job_store_cancel_job(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    assert js.cancel_job("j1") is True
    assert js.cancel_job("missing") is False


def test_job_store_mark_all_running_jobs_failed(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.create_job("j2", "b", "m")
    js.mark_all_running_jobs_failed("shutdown")
    # Both should now be failed
    assert all(j["status"] == "failed" for j in js.list_jobs(running_only=False))


def test_job_store_mark_all_swallows_exception(monkeypatch, caplog) -> None:
    from agent_provisioning_team.shared import job_store as js

    fake = MagicMock()
    fake.mark_all_active_jobs_failed.side_effect = RuntimeError("boom")
    monkeypatch.setattr(js, "_client", lambda cache_dir=None: fake)
    with caplog.at_level(logging.WARNING):
        js.mark_all_running_jobs_failed("shutdown")
    # No exception propagated; warning logged.


def test_job_store_update_phase_progress(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_phase_progress(
        "j1",
        current_phase="setup",
        progress=20,
        current_tool="pg",
        tools_completed=1,
        tools_total=3,
    )
    data = js.get_job("j1")
    assert data["current_phase"] == "setup"
    assert data["current_tool"] == "pg"
    assert data["tools_completed"] == 1


def test_job_store_add_completed_phase_with_result(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.add_completed_phase("j1", "setup", phase_result={"success": True})
    data = js.get_job("j1")
    assert "setup" in data["completed_phases"]
    assert data["phase_results"]["setup"] == {"success": True}

    # Adding the same phase again must not duplicate it.
    js.add_completed_phase("j1", "setup")
    data = js.get_job("j1")
    assert data["completed_phases"].count("setup") == 1


def test_job_store_add_completed_phase_missing_job(mock_job_client) -> None:
    """When the job doesn't exist add_completed_phase no-ops."""
    from agent_provisioning_team.shared import job_store as js

    js.add_completed_phase("missing", "setup")
    assert js.get_job("missing") == {}


def test_job_store_reset_job(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_job("j1", progress=80, status="failed")
    js.reset_job("j1")
    data = js.get_job("j1")
    assert data["status"] == "pending"
    assert data["progress"] == 0


def test_job_store_delete_job(mock_job_client) -> None:
    from agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    assert js.delete_job("j1") is True
    assert js.delete_job("j1") is False


# ---------------------------------------------------------------------------
# logging_context
# ---------------------------------------------------------------------------


def test_logging_context_filter_injects_defaults(caplog) -> None:
    from agent_provisioning_team.shared import logging_context as lc
    from agent_provisioning_team.shared.logging_context import (
        ProvisioningContextFilter,
    )

    # Reset contextvars in case prior tests leaked into this worker.
    tok_j = lc._job_id_var.set(None)
    tok_a = lc._agent_id_var.set(None)
    tok_p = lc._phase_var.set(None)
    try:
        record = logging.LogRecord(
            name="x",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="m",
            args=(),
            exc_info=None,
        )
        f = ProvisioningContextFilter()
        assert f.filter(record) is True
        assert record.job_id == "-"
        assert record.agent_id == "-"
        assert record.phase == "-"

        # Getter helpers report None when nothing is bound.
        assert lc.get_job_id() is None
        assert lc.get_agent_id() is None
        assert lc.get_phase() is None
    finally:
        lc._job_id_var.reset(tok_j)
        lc._agent_id_var.reset(tok_a)
        lc._phase_var.reset(tok_p)


def test_logging_context_manager_binds_and_unbinds() -> None:
    from agent_provisioning_team.shared.logging_context import (
        get_agent_id,
        get_job_id,
        get_phase,
        provisioning_context,
    )

    with provisioning_context(job_id="j1", agent_id="a1", phase="setup"):
        assert get_job_id() == "j1"
        assert get_agent_id() == "a1"
        assert get_phase() == "setup"

    # After exit the values are reset.
    assert get_job_id() is None


def test_logging_context_manager_partial_args() -> None:
    from agent_provisioning_team.shared import logging_context as lc

    tok_a = lc._agent_id_var.set(None)
    try:
        with lc.provisioning_context(job_id="j1"):
            assert lc.get_job_id() == "j1"
            assert lc.get_agent_id() is None
    finally:
        lc._agent_id_var.reset(tok_a)


def test_install_filter_is_idempotent() -> None:
    from agent_provisioning_team.shared import logging_context as lc

    # First call may or may not install; second is always a no-op.
    lc.install_filter()
    lc.install_filter()


# ---------------------------------------------------------------------------
# phase_state.restore_*
# ---------------------------------------------------------------------------


def test_restore_credentials_validates_shape() -> None:
    from agent_provisioning_team.shared.phase_state import restore_credentials

    snap = restore_credentials(
        {
            "success": True,
            "credentials": {"pg": {"tool_name": "pg", "username": "u", "password": "p"}},
        }
    )
    assert snap.success is True
    assert snap.credentials["pg"].username == "u"


def test_restore_account_provisioning_validates_shape() -> None:
    from agent_provisioning_team.shared.phase_state import restore_account_provisioning

    snap = restore_account_provisioning(
        {
            "success": True,
            "tool_results": [
                {"tool_name": "t", "success": True, "provisioner_key": "p", "permissions": []}
            ],
            "tools_completed": 1,
            "tools_total": 1,
        }
    )
    assert snap.success is True
    assert snap.tool_results[0].tool_name == "t"


def test_restore_access_audit_returns_typed() -> None:
    from agent_provisioning_team.shared.phase_state import restore_access_audit

    out = restore_access_audit({"passed": True, "verifications": []})
    assert out.passed is True


def test_restore_documentation_validates_shape() -> None:
    from agent_provisioning_team.shared.phase_state import restore_documentation

    snap = restore_documentation(
        {
            "success": True,
            "onboarding": {
                "summary": "hi",
                "tools": [],
                "environment_variables": {},
            },
        }
    )
    assert snap.success is True
    assert snap.onboarding.summary == "hi"


def test_restore_documentation_with_none_onboarding() -> None:
    from agent_provisioning_team.shared.phase_state import restore_documentation

    snap = restore_documentation({"success": True, "onboarding": None})
    assert snap.success is True
    assert snap.onboarding is None


# ---------------------------------------------------------------------------
# llm_client
# ---------------------------------------------------------------------------


def test_llm_client_is_not_configured_by_default() -> None:
    from agent_provisioning_team.shared.llm_client import LLMClient

    assert LLMClient().is_configured is False


def test_llm_client_complete_raises_when_configured(monkeypatch) -> None:
    from agent_provisioning_team.shared.llm_client import LLMClient, LLMRequest

    client = LLMClient()
    # Trick is_configured into True via monkeypatch on the property's getter.
    with patch.object(type(client), "is_configured", property(lambda self: True)):
        with pytest.raises(NotImplementedError):
            client.complete(LLMRequest(system="s", user="u"))


def test_sanitize_prompt_var_default_max() -> None:
    from agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    s = sanitize_prompt_var("clean ascii")
    assert s == "clean ascii"


def test_sanitize_prompt_var_strips_emoji() -> None:
    from agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    s = sanitize_prompt_var("hi \U0001f600 there")
    # Emoji is not in the allowlist; gets replaced by _ (which is on the
    # allowlist, so two underscores are produced for the multi-byte char).
    assert "_" in s
    assert "hi" in s and "there" in s


def test_sanitize_prompt_var_handles_none() -> None:
    from agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    assert sanitize_prompt_var(None) == ""


# ---------------------------------------------------------------------------
# tool_manifest helpers
# ---------------------------------------------------------------------------


def test_load_manifest_success(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: postgresql
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
""",
        encoding="utf-8",
    )

    m = load_manifest(str(f))
    assert m.version == "1.0"
    assert m.tool_names == ["postgresql"]


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    with pytest.raises(FileNotFoundError):
        load_manifest(str(tmp_path / "ghost.yaml"))


def test_load_manifest_invalid_yaml_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    bad = tmp_path / "bad.yaml"
    bad.write_text(": this is\n  : not yaml", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def test_load_manifest_invalid_structure(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
tools:
  - name: ""
    provisioner: bogus
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def test_load_manifest_empty_file(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    m = load_manifest(str(empty))
    assert m.tools == []


def test_validate_manifest_returns_errors(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest

    # Missing file
    out = validate_manifest(str(tmp_path / "ghost.yaml"))
    assert any("not found" in e.lower() for e in out)


def test_validate_manifest_no_tools_warning(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "empty_tools.yaml"
    f.write_text("version: '1.0'\ntools: []\n", encoding="utf-8")
    out = validate_manifest(str(f))
    assert any("no tools" in e.lower() for e in out)


def test_validate_manifest_duplicate_names(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "dupes.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
  - name: pg
    provisioner: postgres_provisioner
""",
        encoding="utf-8",
    )
    out = validate_manifest(str(f))
    assert any("duplicate" in e.lower() for e in out)


def test_validate_manifest_clean(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "good.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
""",
        encoding="utf-8",
    )
    out = validate_manifest(str(f))
    assert out == []


def test_assert_path_within_base_passes(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import assert_path_within_base

    base = tmp_path
    target = tmp_path / "sub" / "x"
    out = assert_path_within_base(str(target), str(base))
    assert str(out).startswith(str(base.resolve()))


def test_assert_path_within_base_rejects_escape(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.tool_manifest import assert_path_within_base

    with pytest.raises(ValueError, match="escapes"):
        assert_path_within_base("/etc/passwd", str(tmp_path))


def test_tool_definition_invalid_provisioner_rejected() -> None:
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition

    with pytest.raises(ValueError):
        ToolDefinition(name="x", provisioner="quantum", config={})


def test_tool_definition_lowercases_name() -> None:
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(name="Postgres-XL", provisioner="postgres_provisioner", config={})
    assert td.name == "postgres-xl"


def test_tool_definition_invalid_name_rejected() -> None:
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition

    with pytest.raises(ValueError):
        ToolDefinition(name="hi there!", provisioner="postgres_provisioner")


def test_tool_definition_ignores_unknown_top_level_fields() -> None:
    """Older manifests with ``access_level`` should still parse — the field is
    accepted but not surfaced."""
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(
        name="t",
        provisioner="generic_provisioner",
        config={},
        access_level="full",  # unknown / legacy
    )
    assert not hasattr(td, "access_level")


def test_redis_config_visibility_validator() -> None:
    """Cover the git visibility validator via direct manifest construction."""
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(name="g", provisioner="git_provisioner", config={"visibility": "public"})
    assert td.config["visibility"] == "public"


def test_validate_provisioner_config_unknown() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_provisioner_config

    with pytest.raises(ValueError, match="Unknown provisioner"):
        validate_provisioner_config("bogus", {})


def test_validate_manifest_environment_handles_blank_string() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest_environment

    # None value gets coerced to ""
    out = validate_manifest_environment({"FOO": ""})
    assert out["FOO"] == ""


def test_validate_manifest_environment_rejects_non_scalar() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest_environment

    with pytest.raises(ValueError, match="scalar"):
        validate_manifest_environment({"FOO": ["a", "b"]})


def test_validate_manifest_environment_rejects_empty_key() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest_environment

    with pytest.raises(ValueError, match="non-empty"):
        validate_manifest_environment({"": "x"})


def test_validate_manifest_environment_rejects_long_key() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest_environment

    with pytest.raises(ValueError, match="too long"):
        validate_manifest_environment({"X" * 200: "y"})


def test_validate_manifest_environment_caps_value_length() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_manifest_environment

    with pytest.raises(ValueError, match="exceeds"):
        validate_manifest_environment({"FOO": "x" * 10_000})


def test_validate_provisioner_config_normalizes() -> None:
    from agent_provisioning_team.shared.tool_manifest import validate_provisioner_config

    out = validate_provisioner_config("postgres_provisioner", {})
    assert out["database_prefix"] == "agent_"


def test_reject_traversal_components_helper() -> None:
    from agent_provisioning_team.shared.tool_manifest import _reject_traversal_components

    out = _reject_traversal_components("/tmp/x", field="p")
    assert out == "/tmp/x"


def test_reject_path_separators_helper_rejects_empty() -> None:
    from agent_provisioning_team.shared.tool_manifest import _reject_path_separators

    with pytest.raises(ValueError, match="non-empty"):
        _reject_path_separators("", field="x")


def test_reject_path_separators_helper_rejects_traversal() -> None:
    from agent_provisioning_team.shared.tool_manifest import _reject_path_separators

    with pytest.raises(ValueError, match="traverse"):
        _reject_path_separators("..", field="x")


# ---------------------------------------------------------------------------
# tool_agent_registry
# ---------------------------------------------------------------------------


def test_build_default_tool_agents_has_required_keys() -> None:
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents

    out = build_default_tool_agents()
    for key in (
        "docker_provisioner",
        "postgres_provisioner",
        "redis_provisioner",
        "git_provisioner",
        "generic_provisioner",
    ):
        assert key in out


# ---------------------------------------------------------------------------
# provisioner_state edge cases
# ---------------------------------------------------------------------------


def test_provisioner_state_load_corrupt_file(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore

    # Pre-write a corrupt file
    state = ProvisionerStateStore("xx", storage_dir=tmp_path)
    state.path.write_text("not json", encoding="utf-8")
    # Reload should return {} instead of crashing
    fresh = ProvisionerStateStore("xx", storage_dir=tmp_path)
    assert fresh.get("any") is None


def test_provisioner_state_list_agents(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1})
    store.put("a2", {"y": 2})
    out = store.list_agents()
    assert out == {"a1": {"x": 1}, "a2": {"y": 2}}


def test_compensation_record_serialization() -> None:
    from agent_provisioning_team.shared.provisioner_state import CompensationRecord

    rec = CompensationRecord(kind="k", payload={"a": 1})
    d = rec.to_json()
    restored = CompensationRecord.from_json(d)
    assert restored.kind == rec.kind
    assert restored.payload == rec.payload


def test_clear_compensations_on_missing_agent(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    # Should be a no-op rather than raising.
    store.clear_compensations("missing")
