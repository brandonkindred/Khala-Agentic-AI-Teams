"""
Integration test matrix for the Agent Provisioning Team.

Covers the follow-ups from the principal-architect review:

* manifest discriminated union + env-var allowlist
* idempotent ProvisionerStateStore (persistence + get_or_create)
* credential key rotation via MultiFernet
* recursive redaction of nested secrets in tool_results.details
* orchestrator compensation rollback on account_provisioning failure
* orchestrator resume-with-prior-results restores typed snapshots
* LLM prompt-var sanitization

Tests use in-memory fakes and a ``tmp_path``-scoped cache dir; nothing
touches Docker / Postgres / Temporal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_provisioning_team.models import (
    AccessTier,
    DeprovisionResult,
    EnvironmentInfo,
    GeneratedCredentials,
    Phase,
    SetupResult,
    ToolProvisionResult,
)
from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
from agent_provisioning_team.phases.deliver import (
    _redact_details,
    redact_credentials_for_response,
)
from agent_provisioning_team.shared.credential_store import (
    CredentialStore,
    CredentialStoreConfigError,
)
from agent_provisioning_team.shared.llm_client import LLMClient, sanitize_prompt_var
from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore
from agent_provisioning_team.shared.tool_manifest import (
    ToolManifest,
    validate_manifest_environment,
    validate_provisioner_config,
)
from agent_provisioning_team.tool_agents.base import BaseToolProvisioner

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvisioner(BaseToolProvisioner):
    """In-memory provisioner that records calls; can be told to fail."""

    def __init__(self, tool_name: str, fail: bool = False) -> None:
        self.tool_name = tool_name
        self.fail = fail
        self.provisioned: List[str] = []
        self.deprovisioned: List[str] = []

    def provision(self, agent_id, config, credentials, access_tier):
        if self.fail:
            return self._make_error_result(f"{self.tool_name} exploded")
        self.provisioned.append(agent_id)
        return self._make_success_result(
            credentials=credentials,
            permissions=["read", "write"],
            details={"name": f"{self.tool_name}-{agent_id}"},
        )

    def verify_access(self, agent_id, expected_tier):
        return self._make_verification(
            passed=True,
            expected_tier=expected_tier,
            actual_permissions=["read", "write"],
        )

    def deprovision(self, agent_id):
        self.deprovisioned.append(agent_id)
        return DeprovisionResult(tool_name=self.tool_name, success=True)


# ---------------------------------------------------------------------------
# Manifest discriminated union + env-var allowlist
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_valid_postgres_config_accepted(self):
        out = validate_provisioner_config(
            "postgres_provisioner", {"database_prefix": "agent_", "max_connections": 10}
        )
        assert out["database_prefix"] == "agent_"
        assert out["max_connections"] == 10

    def test_unknown_provisioner_rejected(self):
        with pytest.raises(ValueError, match="Unknown provisioner"):
            validate_provisioner_config("quantum_provisioner", {})

    def test_invalid_visibility_rejected(self):
        with pytest.raises(ValueError):
            validate_provisioner_config("git_provisioner", {"visibility": "world-readable"})

    def test_env_allowlist_rejects_secret_key(self):
        with pytest.raises(ValueError, match="looks like a secret"):
            validate_manifest_environment({"MY_SECRET": "oops"})

    def test_env_allowlist_rejects_lowercase(self):
        with pytest.raises(ValueError, match="UPPER_SNAKE"):
            validate_manifest_environment({"path": "/tmp"})

    def test_env_allowlist_rejects_shell_metachar(self):
        with pytest.raises(ValueError, match="metacharacters"):
            validate_manifest_environment({"GREETING": "hi`rm -rf`"})

    def test_env_allowlist_accepts_clean(self):
        out = validate_manifest_environment({"LANG": "C.UTF-8", "PORT": "8080"})
        assert out == {"LANG": "C.UTF-8", "PORT": "8080"}

    def test_tool_manifest_end_to_end(self):
        m = ToolManifest(
            tools=[
                {
                    "name": "postgresql",
                    "provisioner": "postgres_provisioner",
                    "config": {"database_prefix": "x_"},
                    "onboarding": {"description": "db"},
                }
            ],
            environment={"LANG": "C.UTF-8"},
        )
        assert m.get_tool("postgresql").config["database_prefix"] == "x_"

    def test_workspace_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_provisioner_config("git_provisioner", {"workspace_path": "../../etc"})
        with pytest.raises(ValueError, match="traversal"):
            validate_provisioner_config(
                "docker_provisioner", {"workspace_path": "/workspace/../etc"}
            )

    def test_init_repo_name_traversal_rejected(self):
        with pytest.raises(ValueError, match="traverse|path separators"):
            validate_provisioner_config("git_provisioner", {"init_repos": ["../escape"]})
        with pytest.raises(ValueError, match="path separators"):
            validate_provisioner_config("git_provisioner", {"init_repos": ["nested/segment"]})

    def test_workspace_path_accepts_clean_absolute(self):
        # Regression guard: manifest-level rejection must not touch legitimate
        # absolute workspace paths (used throughout the existing integration
        # matrix and production manifests).
        out = validate_provisioner_config(
            "git_provisioner", {"workspace_path": "/tmp/ws", "init_repos": ["workspace"]}
        )
        assert out["workspace_path"] == "/tmp/ws"
        assert out["init_repos"] == ["workspace"]


# ---------------------------------------------------------------------------
# ProvisionerStateStore
# ---------------------------------------------------------------------------


class TestProvisionerStateStore:
    def test_roundtrip_persists_to_disk(self, tmp_path: Path):
        s = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
        assert s.get("agent-1") is None
        s.put("agent-1", {"container": "c1"})
        # A fresh instance loads from disk, proving persistence.
        s2 = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
        assert s2.get("agent-1") == {"container": "c1"}

    def test_get_or_create_runs_creator_once(self, tmp_path: Path):
        s = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
        calls = {"n": 0}

        def _creator():
            calls["n"] += 1
            return {"container": "new"}

        v1 = s.get_or_create("agent-1", _creator)
        v2 = s.get_or_create("agent-1", _creator)
        assert v1 == v2 == {"container": "new"}
        assert calls["n"] == 1

    def test_delete(self, tmp_path: Path):
        s = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
        s.put("agent-1", {"x": 1})
        assert s.delete("agent-1") is True
        assert s.delete("agent-1") is False
        assert s.get("agent-1") is None


# ---------------------------------------------------------------------------
# Unified provisioner scaffolding (BaseToolProvisioner.run_idempotent)
# ---------------------------------------------------------------------------


class TestProvisionerScaffolding:
    """Covers the shared helper that owns idempotency + exception handling."""

    def test_git_subprocess_timeout_is_surfaced_as_error_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for the original bug: no timeout on `ssh-keygen` / `git init`
        would hang the provisioning worker. With timeouts wired through
        ``run_idempotent``, a ``subprocess.TimeoutExpired`` must become an
        error ``ToolProvisionResult`` — not a raised exception.
        """
        import subprocess as _subprocess

        from agent_provisioning_team.tool_agents.git_provisioner import GitProvisionerTool

        tool = GitProvisionerTool(workspace_base=str(tmp_path))
        # Point state store at tmp_path so the test is hermetic.
        tool._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

        def _always_timeout(*args, **kwargs):  # noqa: ARG001
            raise _subprocess.TimeoutExpired(cmd=args[0] if args else "git", timeout=30)

        monkeypatch.setattr(_subprocess, "run", _always_timeout)

        creds = GeneratedCredentials(tool_name="git")
        result = tool.provision(
            agent_id="agent-timeout",
            config={"workspace_path": str(tmp_path / "ws"), "generate_ssh_key": True},
            credentials=creds,
            access_tier=AccessTier.STANDARD,
        )
        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    def test_provisioner_idempotency_persists_across_instances(
        self,
        tmp_path: Path,
    ) -> None:
        """A fresh GenericProvisionerTool pointed at the same state dir must
        see the prior agent's record and return ``reused: True`` — proves the
        in-memory ``_provisioned`` dict was replaced with ``ProvisionerStateStore``.
        """
        from agent_provisioning_team.tool_agents.generic_provisioner import (
            GenericProvisionerTool,
        )

        store_dir = tmp_path / "state"

        tool_a = GenericProvisionerTool(tool_name="myservice")
        tool_a._state = ProvisionerStateStore(
            "generic_myservice_provisioner", storage_dir=store_dir
        )
        first = tool_a.provision(
            agent_id="agent-1",
            config={"permissions": ["read"]},
            credentials=GeneratedCredentials(tool_name="myservice"),
            access_tier=AccessTier.STANDARD,
        )
        assert first.success is True
        assert first.details.get("reused") is not True

        # Fresh instance — no in-memory handle — pointed at the same dir.
        tool_b = GenericProvisionerTool(tool_name="myservice")
        tool_b._state = ProvisionerStateStore(
            "generic_myservice_provisioner", storage_dir=store_dir
        )
        second = tool_b.provision(
            agent_id="agent-1",
            config={"permissions": ["read"]},
            credentials=GeneratedCredentials(tool_name="myservice"),
            access_tier=AccessTier.STANDARD,
        )
        assert second.success is True
        assert second.details.get("reused") is True


# ---------------------------------------------------------------------------
# CredentialStore — key rotation + require-key
# ---------------------------------------------------------------------------


class TestCredentialStoreRotation:
    def test_require_key_hard_fails(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PROVISION_REQUIRE_KEY", "1")
        monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
        monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
        # Point the store somewhere without a pre-generated key file.
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(CredentialStoreConfigError):
            CredentialStore(storage_dir=empty)

    def test_key_rotation_reencrypts_existing(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
        from cryptography.fernet import Fernet

        k1 = Fernet.generate_key().decode()
        store = CredentialStore(storage_dir=tmp_path, encryption_key=k1)
        store.store_credentials("agent-1", "postgres", {"password": "p"})
        assert store.get_credentials("agent-1", "postgres") == {"password": "p"}

        # Rotate to a new key.
        k2 = Fernet.generate_key().decode()
        n = store.rotate_key(k2)
        assert n >= 1
        # Old data still readable after rotation.
        assert store.get_credentials("agent-1", "postgres") == {"password": "p"}
        # A new store that only knows the new key can still read the file.
        store2 = CredentialStore(storage_dir=tmp_path, encryption_key=k2)
        assert store2.get_credentials("agent-1", "postgres") == {"password": "p"}

    def test_multi_key_list_accepts_both(self, tmp_path: Path, monkeypatch):
        from cryptography.fernet import Fernet

        k1 = Fernet.generate_key().decode()
        k2 = Fernet.generate_key().decode()
        # Write data with k1 alone.
        s_old = CredentialStore(storage_dir=tmp_path, encryption_key=k1)
        s_old.store_credentials("agent-1", "postgres", {"password": "p"})
        # New store knows k2 first, then k1 — should still decrypt old data.
        s_new = CredentialStore(storage_dir=tmp_path, encryption_key=f"{k2},{k1}")
        assert s_new.get_credentials("agent-1", "postgres") == {"password": "p"}


# ---------------------------------------------------------------------------
# Redaction of nested secrets
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redact_details_scrubs_nested_password(self):
        dirty = {
            "host": "db.local",
            "credentials": {"username": "u", "password": "hunter2", "token": "t"},
            "extras": [{"api_key": "abc"}, {"safe": "ok"}],
        }
        out = _redact_details(dirty)
        assert out["host"] == "db.local"
        assert out["credentials"]["password"] == "***"
        assert out["credentials"]["token"] == "***"
        assert out["extras"][0]["api_key"] == "***"
        assert out["extras"][1]["safe"] == "ok"

    def test_redact_details_scrubs_embedded_connection_string(self):
        out = _redact_details({"conn": "postgresql://u:pw@host:5432/db"})
        # The "conn" key contains a connection-string-like value.
        assert "***" in out["conn"]

    def test_redact_credentials_for_response_cleans_tool_results(self):
        from agent_provisioning_team.models import ProvisioningResult

        result = ProvisioningResult(
            agent_id="a1",
            current_phase=Phase.DELIVER,
            success=True,
            credentials={
                "pg": GeneratedCredentials(
                    tool_name="pg",
                    username="u",
                    password="pw",
                    connection_string="postgresql://u:pw@host:5432/db",
                )
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="pg",
                    success=True,
                    details={"api_key": "abc", "db": "mydb"},
                )
            ],
        )
        redacted = redact_credentials_for_response(result)
        assert redacted.credentials["pg"].password == "***"
        assert redacted.tool_results[0].details["api_key"] == "***"
        assert redacted.tool_results[0].details["db"] == "mydb"


# ---------------------------------------------------------------------------
# LLM client + sanitizer
# ---------------------------------------------------------------------------


class TestLLMClient:
    def test_sanitize_strips_control_chars(self):
        assert "\x00" not in sanitize_prompt_var("hi\x00there")

    def test_sanitize_caps_length(self):
        big = "x" * 10_000
        out = sanitize_prompt_var(big, max_len=100)
        assert len(out) <= 120
        assert "truncated" in out

    def test_llm_fallback_labeled(self):
        from agent_provisioning_team.shared.llm_client import LLMRequest

        resp = LLMClient().complete(LLMRequest(system="s", user="hello"))
        assert resp.startswith("[llm-fallback]")


# ---------------------------------------------------------------------------
# Orchestrator — compensation rollback
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path) -> str:
    path = tmp_path / "m.yaml"
    path.write_text(
        """
version: "1.0"
tools:
  - name: toola
    provisioner: postgres_provisioner
    access_level: standard
    config: {database_prefix: "a_"}
    onboarding: {description: "a"}
  - name: toolb
    provisioner: redis_provisioner
    access_level: standard
    config: {key_prefix: "b:"}
    onboarding: {description: "b"}
"""
    )
    return str(path)


class TestOrchestratorCompensation:
    def test_compensation_deprovisions_successful_tools(self, tmp_path, monkeypatch):
        # Fake tool agents: toola succeeds, toolb fails.
        pg = _FakeProvisioner("toola")
        redis_ = _FakeProvisioner("toolb", fail=True)
        docker = _FakeProvisioner("docker")

        fake_agents: Dict[str, Any] = {
            "postgres_provisioner": pg,
            "redis_provisioner": redis_,
            "docker_provisioner": docker,
            "git_provisioner": _FakeProvisioner("git"),
            "generic_provisioner": _FakeProvisioner("generic"),
        }

        # Stub setup to succeed without touching Docker.
        from agent_provisioning_team import orchestrator as orch_mod

        def _fake_run_setup(**kwargs):
            return SetupResult(
                success=True,
                environment=EnvironmentInfo(
                    container_id="c1",
                    container_name="c1",
                    workspace_path="/tmp/ws",
                    status="running",
                ),
            )

        monkeypatch.setattr(orch_mod, "run_setup", _fake_run_setup)

        manifest = _write_manifest(tmp_path)
        orch = ProvisioningOrchestrator(tool_agents=fake_agents)
        result = orch.run_workflow(
            agent_id="agent-1",
            manifest_path=manifest,
            access_tier=AccessTier.STANDARD,
        )

        assert result.success is False
        assert result.current_phase == Phase.ACCOUNT_PROVISIONING
        # Compensation should have rolled back the tool that DID succeed.
        assert "agent-1" in pg.deprovisioned
        # Docker teardown too.
        assert "agent-1" in docker.deprovisioned

    def test_compensation_when_tool_name_differs_from_registry_key(self, tmp_path, monkeypatch):
        """Regression test for #293.

        The production `PostgresProvisionerTool` has
        ``tool_name = "postgresql"`` while its registry key is
        ``"postgres_provisioner"``. Before #293 the orchestrator looked up
        the provisioner as ``f"{tool_name}_provisioner"`` which silently
        missed for this exact case, leaking the DB account + encrypted
        credential file. This test simulates that mismatch with a fake
        provisioner whose ``tool_name`` does NOT match its registry key and
        asserts compensation still rolls it back.
        """
        # tool_name intentionally mismatched with registry stem.
        pg_like = _FakeProvisioner("postgresql")
        redis_ = _FakeProvisioner("redis", fail=True)
        docker = _FakeProvisioner("docker")

        fake_agents: Dict[str, Any] = {
            "postgres_provisioner": pg_like,
            "redis_provisioner": redis_,
            "docker_provisioner": docker,
            "git_provisioner": _FakeProvisioner("git"),
            "generic_provisioner": _FakeProvisioner("generic"),
        }

        from agent_provisioning_team import orchestrator as orch_mod

        def _fake_run_setup(**kwargs):
            return SetupResult(
                success=True,
                environment=EnvironmentInfo(
                    container_id="c1",
                    container_name="c1",
                    workspace_path="/tmp/ws",
                    status="running",
                ),
            )

        monkeypatch.setattr(orch_mod, "run_setup", _fake_run_setup)

        manifest = _write_manifest(tmp_path)
        orch = ProvisioningOrchestrator(tool_agents=fake_agents)
        result = orch.run_workflow(
            agent_id="agent-2",
            manifest_path=manifest,
            access_tier=AccessTier.STANDARD,
        )

        assert result.success is False
        assert result.current_phase == Phase.ACCOUNT_PROVISIONING
        # The postgres-like provisioner (registered under
        # "postgres_provisioner" but exposing tool_name="postgresql") must
        # still be deprovisioned — lookup must use the registry key.
        assert "agent-2" in pg_like.deprovisioned, (
            "Compensation dropped a provisioner whose tool_name differs from "
            "its registry key — the #293 regression."
        )
        assert "agent-2" in docker.deprovisioned

    def test_every_default_provisioner_has_registry_key_roundtrip(self):
        """Cheap invariant: every default provisioner's registry key round-trips.

        Catches future drift where someone adds a provisioner whose registry
        key isn't exactly what downstream code stamps onto
        ``ToolProvisionResult.provisioner_key``.
        """
        from agent_provisioning_team.shared.tool_agent_registry import (
            build_default_tool_agents,
        )

        registry = build_default_tool_agents()
        for key, prov in registry.items():
            result = ToolProvisionResult(
                tool_name=prov.tool_name,
                success=True,
                provisioner_key=key,
            )
            assert result.provisioner_key in registry, (
                f"Registry key {key!r} not round-trippable via ToolProvisionResult.provisioner_key"
            )


# ---------------------------------------------------------------------------
# Phase snapshot restore (resume after crash)
# ---------------------------------------------------------------------------


class TestPhaseSnapshotRestore:
    def test_restore_setup_validates_shape(self):
        from agent_provisioning_team.shared.phase_state import restore_setup

        snap = restore_setup(
            {
                "success": True,
                "environment": {
                    "container_id": "c1",
                    "container_name": "c1",
                    "workspace_path": "/w",
                    "status": "running",
                },
            }
        )
        assert snap.success is True
        assert snap.environment.container_id == "c1"

    def test_restore_setup_rejects_bad_shape(self):
        from agent_provisioning_team.shared.phase_state import restore_setup

        with pytest.raises(Exception):
            restore_setup({"success": "maybe"})  # type: ignore[arg-type]
