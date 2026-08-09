"""Unit tests for the Git provisioner tool agent.

ssh-keygen / git invocations are mocked at subprocess.run boundary so no real
binaries are exercised. Some tests do touch the filesystem under tmp_path.
"""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
from agent_team_studio.agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore
from agent_team_studio.agent_provisioning_team.tool_agents.git_provisioner import GitProvisionerTool


def _ok(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_git_provision_creates_workspace_and_repo(tmp_path: Path) -> None:
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    prov = GitProvisionerTool(workspace_base=str(ws_base))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    creds = GeneratedCredentials(tool_name="git")

    def _ssh_keygen(*args, **kwargs):
        # Simulate ssh-keygen creating the keys.
        cmd = args[0]
        if cmd[0] == "ssh-keygen":
            # Find the -f path and write fake key files there.
            idx = cmd.index("-f")
            key_path = Path(cmd[idx + 1])
            key_path.write_text("FAKE_PRIVATE\n")
            key_path.with_suffix(key_path.suffix + ".pub").write_text("ssh-ed25519 FAKEPUB\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return _ok(*args, **kwargs)

    with patch("subprocess.run", side_effect=_ssh_keygen):
        result = prov.provision(
            "agent-1",
            {
                "workspace_path": str(ws_base / "agent-1"),
                "init_repos": ["workspace"],
                "generate_ssh_key": True,
            },
            creds,
        )

    assert result.success is True
    assert creds.ssh_private_key.startswith("FAKE_PRIVATE")
    assert "id_ed25519" in result.details["ssh_key_path"]


def test_git_provision_skips_keygen_when_disabled(tmp_path: Path) -> None:
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    prov = GitProvisionerTool(workspace_base=str(ws_base))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    with patch("subprocess.run", side_effect=_ok):
        result = prov.provision(
            "agent-1",
            {
                "workspace_path": str(ws_base / "agent-1"),
                "init_repos": ["workspace"],
                "generate_ssh_key": False,
            },
            GeneratedCredentials(tool_name="git"),
        )

    assert result.success is True
    assert result.details["ssh_key_path"] is None
    assert result.details["ssh_key_generated"] is False


def test_git_provision_handles_init_repo_failure(tmp_path: Path) -> None:
    """A failing `git init` should mark the repo as not initialized but the
    overall provision still succeeds (workspace + config get written)."""
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    prov = GitProvisionerTool(workspace_base=str(ws_base))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    def _flaky(*args, **kwargs):
        cmd = args[0]
        if cmd and cmd[0] == "git" and cmd[1] == "init":
            raise _subprocess.CalledProcessError(returncode=1, cmd=cmd)
        if cmd[0] == "ssh-keygen":
            idx = cmd.index("-f")
            key_path = Path(cmd[idx + 1])
            key_path.write_text("PRIV\n")
            key_path.with_suffix(key_path.suffix + ".pub").write_text("PUB\n")
            return _ok(*args, **kwargs)
        return _ok(*args, **kwargs)

    with patch("subprocess.run", side_effect=_flaky):
        result = prov.provision(
            "agent-1",
            {
                "workspace_path": str(ws_base / "agent-1"),
                "init_repos": ["workspace"],
                "generate_ssh_key": True,
            },
            GeneratedCredentials(tool_name="git"),
        )

    assert result.success is True
    assert result.details["repos"] == []  # repo init failed


def test_git_provision_workspace_escape_rejected(tmp_path: Path) -> None:
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    prov = GitProvisionerTool(workspace_base=str(ws_base))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    # workspace_path outside workspace_base must trigger assert_path_within_base
    out = prov.provision(
        "agent-1",
        {"workspace_path": str(tmp_path / "elsewhere" / "x")},
        GeneratedCredentials(tool_name="git"),
    )
    assert out.success is False
    assert "escapes" in out.error or "workspace" in out.error.lower()


def test_git_init_repo_skip_when_already_initialized(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # pretend already a git repo

    # Should return True without invoking subprocess.run
    with patch("subprocess.run") as mock_run:
        result = prov._init_repo(repo)

    assert result is True
    mock_run.assert_not_called()


def test_git_verify_access_no_state(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    v = prov.verify_access("missing")
    assert v.passed is False
    assert "No Git provisioning" in v.errors[0]


def test_git_verify_access_workspace_present(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    ws = tmp_path / "agent-1"
    ws.mkdir()
    repo = ws / "workspace"
    repo.mkdir()
    (repo / ".git").mkdir()

    prov._state.put(
        "agent-1",
        {
            "workspace_path": str(ws),
            "repos": [str(repo)],
            "ssh_key_path": str(ws / ".ssh" / "id_ed25519"),
            "permissions": ["read", "write"],
        },
    )

    v = prov.verify_access("agent-1")
    assert v.passed is True
    assert v.errors == []


def test_git_verify_access_workspace_missing(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    prov._state.put(
        "agent-1",
        {
            "workspace_path": "/nonexistent/path",
            "repos": ["/nonexistent/path/repo"],
            "ssh_key_path": None,
            "permissions": ["read"],
        },
    )

    v = prov.verify_access("agent-1")
    assert v.passed is False
    assert any("Workspace directory not found" in e for e in v.errors)


def test_git_verify_access_repo_missing_is_warning(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    ws = tmp_path / "agent-x"
    ws.mkdir()
    # Repo dir doesn't actually have a .git inside
    missing_repo = ws / "norepo"
    missing_repo.mkdir()

    prov._state.put(
        "agent-x",
        {
            "workspace_path": str(ws),
            "repos": [str(missing_repo)],
            "ssh_key_path": None,
            "permissions": ["read"],
        },
    )

    v = prov.verify_access("agent-x")
    # Workspace exists so passed=True but warnings list the missing repo.
    assert v.passed is True
    assert any("not initialized" in w for w in v.warnings)


def test_git_deprovision_no_state(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("missing")
    assert out.success is True
    assert "No Git" in out.details["message"]


def test_git_deprovision_removes_ssh_keys(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    priv = ssh_dir / "id_ed25519"
    pub = ssh_dir / "id_ed25519.pub"
    priv.write_text("priv")
    pub.write_text("pub")

    prov._state.put(
        "a1",
        {
            "workspace_path": str(tmp_path),
            "repos": [],
            "ssh_key_path": str(priv),
            "permissions": [],
        },
    )

    out = prov.deprovision("a1")
    assert out.success is True
    assert not priv.exists()
    assert not pub.exists()


def test_git_deprovision_no_ssh_keys(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    prov._state.put(
        "a1",
        {"workspace_path": str(tmp_path), "repos": [], "ssh_key_path": None, "permissions": []},
    )

    out = prov.deprovision("a1")
    assert out.success is True
    assert out.details["ssh_keys_removed"] is False


def test_git_deprovision_handles_exception(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)
    prov._state.put(
        "a1",
        {"workspace_path": str(tmp_path), "repos": [], "ssh_key_path": None, "permissions": []},
    )

    with patch.object(prov._state, "delete", side_effect=RuntimeError("io")):
        out = prov.deprovision("a1")
    assert out.success is False
    assert "io" in out.error


def test_git_provision_reuse_path(tmp_path: Path) -> None:
    """Second call with existing state must hydrate workspace_path + repos."""
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    prov = GitProvisionerTool(workspace_base=str(ws_base))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    prov._state.put(
        "a1",
        {
            "workspace_path": str(ws_base / "a1"),
            "repos": [str(ws_base / "a1" / "workspace")],
            "permissions": ["read"],
        },
    )

    creds = GeneratedCredentials(tool_name="git")
    out = prov.provision("a1", {}, creds)
    assert out.success is True
    assert creds.extra["workspace_path"] == str(ws_base / "a1")


def test_git_generate_ssh_keypair_removes_existing(tmp_path: Path) -> None:
    prov = GitProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("git_provisioner", storage_dir=tmp_path)

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    priv = ssh_dir / "id_ed25519"
    pub = ssh_dir / "id_ed25519.pub"
    priv.write_text("OLD_PRIV")
    pub.write_text("OLD_PUB")

    def _fake_keygen(*args, **kwargs):
        cmd = args[0]
        # Honor the actual delete-and-rewrite flow by writing fresh content.
        idx = cmd.index("-f")
        key_path = Path(cmd[idx + 1])
        key_path.write_text("NEW_PRIV")
        key_path.with_suffix(key_path.suffix + ".pub").write_text("NEW_PUB")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_fake_keygen):
        prv_text, pub_text = prov._generate_ssh_keypair("agent-x", ssh_dir)
    assert prv_text == "NEW_PRIV"
    assert pub_text == "NEW_PUB"
