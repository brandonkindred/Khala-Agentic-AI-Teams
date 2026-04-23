"""
Git provisioner tool agent.

Sets up Git configuration, SSH keys, and initializes repositories.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.access_policy import get_permissions
from ..shared.provisioner_state import ProvisionerStateStore
from ..shared.tool_manifest import assert_path_within_base
from .base import BaseToolProvisioner

# Every git subprocess gets bounded wall-clock; unbounded calls could hang the
# provisioning worker if the git remote or filesystem stalled. Matches the
# timeout style already used in docker_provisioner.py.
_GIT_SUBPROCESS_TIMEOUT_S = 30


class GitProvisionerTool(BaseToolProvisioner):
    """Tool agent for Git provisioning."""

    tool_name = "git"

    def __init__(self, workspace_base: str = "/workspace") -> None:
        self.workspace_base = workspace_base
        self._state = ProvisionerStateStore("git_provisioner")

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Set up Git for the agent with optional SSH keys and repos."""
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda: self._do_provision(agent_id, config, credentials, access_tier),
            hydrate_extras=("workspace_path", "repos"),
        )

    def _do_provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> Tuple[List[str], Dict[str, Any]]:
        workspace_path = config.get("workspace_path", f"{self.workspace_base}/{agent_id}")
        init_repos = config.get("init_repos", ["workspace"])
        generate_ssh_key = config.get("generate_ssh_key", True)

        # Defence-in-depth: manifest validation already rejects `..` paths, but
        # the workspace_base (set at provisioner construction time) is only
        # known here, so the containment check runs at provisioning time too.
        # ``init_repos`` entries are separator-free per manifest validation, so
        # they don't need a second pass.
        workspace = assert_path_within_base(workspace_path, self.workspace_base)
        workspace.mkdir(parents=True, exist_ok=True)

        ssh_dir = workspace / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)

        if generate_ssh_key:
            private_key, public_key = self._generate_ssh_keypair(agent_id, ssh_dir)
            credentials.ssh_private_key = private_key
            credentials.ssh_public_key = public_key

        git_config_path = workspace / ".gitconfig"
        git_config_path.write_text(
            f"""[user]
    name = Agent {agent_id}
    email = agent-{agent_id}@provisioning.local
[init]
    defaultBranch = main
[core]
    autocrlf = input
    editor = vim
"""
        )

        initialized_repos: List[str] = []
        for repo_name in init_repos:
            repo_path = workspace / repo_name
            if self._init_repo(repo_path):
                initialized_repos.append(str(repo_path))

        permissions = get_permissions("git", access_tier)

        credentials.extra["workspace_path"] = str(workspace)
        credentials.extra["git_config"] = str(git_config_path)
        credentials.extra["repos"] = initialized_repos

        details = {
            "workspace_path": str(workspace),
            "repos": initialized_repos,
            "repos_initialized": initialized_repos,
            "ssh_key_path": str(ssh_dir / "id_ed25519") if generate_ssh_key else None,
            "ssh_key_generated": generate_ssh_key,
            "permissions": permissions,
        }
        return permissions, details

    def _generate_ssh_keypair(
        self,
        agent_id: str,
        ssh_dir: Path,
    ) -> Tuple[str, str]:
        """Generate an ED25519 SSH keypair."""
        private_key_path = ssh_dir / "id_ed25519"
        public_key_path = ssh_dir / "id_ed25519.pub"

        if private_key_path.exists():
            private_key_path.unlink()
        if public_key_path.exists():
            public_key_path.unlink()

        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-C",
                f"agent-{agent_id}@provisioning.local",
                "-f",
                str(private_key_path),
                "-N",
                "",
            ],
            check=True,
            capture_output=True,
            timeout=_GIT_SUBPROCESS_TIMEOUT_S,
        )

        private_key_path.chmod(0o600)
        public_key_path.chmod(0o644)

        private_key = private_key_path.read_text()
        public_key = public_key_path.read_text()

        return private_key, public_key

    def _init_repo(self, repo_path: Path) -> bool:
        """Initialize a Git repository."""
        try:
            repo_path.mkdir(parents=True, exist_ok=True)

            git_dir = repo_path / ".git"
            if git_dir.exists():
                return True

            subprocess.run(
                ["git", "init"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
                timeout=_GIT_SUBPROCESS_TIMEOUT_S,
            )

            readme = repo_path / "README.md"
            if not readme.exists():
                readme.write_text(
                    "# Workspace\n\n"
                    "Agent workspace repository.\n\n"
                    "After provisioning, see `docs/agent_anatomy/` for the canonical AI agent anatomy "
                    "(AGENT_ANATOMY.md and reference diagrams).\n"
                )

            gitignore = repo_path / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "# Python\n__pycache__/\n*.pyc\n.venv/\n\n"
                    "# IDE\n.idea/\n.vscode/\n\n"
                    "# OS\n.DS_Store\nThumbs.db\n"
                )

            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
                timeout=_GIT_SUBPROCESS_TIMEOUT_S,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
                timeout=_GIT_SUBPROCESS_TIMEOUT_S,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Provisioner",
                    "GIT_AUTHOR_EMAIL": "provisioner@local",
                    "GIT_COMMITTER_NAME": "Provisioner",
                    "GIT_COMMITTER_EMAIL": "provisioner@local",
                },
            )

            return True

        except subprocess.CalledProcessError:
            return False

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify Git access for the agent."""
        prov_info = self._state.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[f"No Git provisioning found for agent {agent_id}"],
            )

        warnings: List[str] = []
        errors: List[str] = []

        workspace = Path(prov_info["workspace_path"])
        if not workspace.exists():
            errors.append(f"Workspace directory not found: {workspace}")

        for repo_path in prov_info.get("repos", []):
            git_dir = Path(repo_path) / ".git"
            if not git_dir.exists():
                warnings.append(f"Git repo not initialized: {repo_path}")

        actual_permissions = prov_info.get("permissions", [])
        passed = len(errors) == 0

        return self._make_verification(
            passed=passed,
            expected_tier=expected_tier,
            actual_permissions=actual_permissions,
            warnings=warnings,
            errors=errors,
        )

    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Clean up Git provisioning (removes SSH keys, keeps repos)."""
        prov_info = self._state.get(agent_id)

        if not prov_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No Git provisioning to remove"},
            )

        try:
            ssh_key_path = prov_info.get("ssh_key_path")
            if ssh_key_path:
                private_key = Path(ssh_key_path)
                public_key = Path(f"{ssh_key_path}.pub")

                if private_key.exists():
                    private_key.unlink()
                if public_key.exists():
                    public_key.unlink()

            self._state.delete(agent_id)

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={
                    "ssh_keys_removed": ssh_key_path is not None,
                    "repos_preserved": prov_info.get("repos", []),
                },
            )

        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )
