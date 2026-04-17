"""Static configuration for the agent sandbox lifecycle.

This is the single source of truth for **which** teams have sandbox support in
Phase 2 (mirrors ``docker/sandbox.compose.yml``) and **what port and service
name** each sandbox maps to. Adding a new team requires one new entry here,
one new service block in the compose file, and one ``mount_invoke_shim()``
call in that team's ``api/main.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TeamSandboxConfig:
    """Sandbox metadata for a single team."""

    team: str
    service_name: str
    container_name: str
    default_host_port: int
    port_env_var: str
    health_path: str = "/health"

    @property
    def host_port(self) -> int:
        return int(os.environ.get(self.port_env_var, str(self.default_host_port)))

    @property
    def url(self) -> str:
        return f"http://localhost:{self.host_port}"


# Teams currently wired for sandbox execution. See the Phase 2 plan in
# docs/architecture for the onboarding checklist.
TEAM_SANDBOX_CONFIGS: dict[str, TeamSandboxConfig] = {
    "blogging": TeamSandboxConfig(
        team="blogging",
        service_name="blogging-sandbox",
        container_name="khala-sandbox-blogging",
        default_host_port=8200,
        port_env_var="BLOGGING_SANDBOX_PORT",
    ),
    "software_engineering": TeamSandboxConfig(
        team="software_engineering",
        service_name="se-sandbox",
        container_name="khala-sandbox-se",
        default_host_port=8201,
        port_env_var="SE_SANDBOX_PORT",
    ),
    "planning_v3": TeamSandboxConfig(
        team="planning_v3",
        service_name="planning-v3-sandbox",
        container_name="khala-sandbox-planning-v3",
        default_host_port=8202,
        port_env_var="PLANNING_V3_SANDBOX_PORT",
    ),
    "branding": TeamSandboxConfig(
        team="branding",
        service_name="branding-sandbox",
        container_name="khala-sandbox-branding",
        default_host_port=8203,
        port_env_var="BRANDING_SANDBOX_PORT",
    ),
}


def compose_file_path() -> Path:
    """Path to ``docker/sandbox.compose.yml`` within the repo."""
    override = os.environ.get("SANDBOX_COMPOSE_FILE")
    if override:
        return Path(override)
    # agents/agent_sandbox/config.py → ../../../docker/sandbox.compose.yml
    return Path(__file__).resolve().parent.parent.parent.parent / "docker" / "sandbox.compose.yml"


def idle_teardown_seconds() -> int:
    """Read ``SANDBOX_IDLE_TEARDOWN_MINUTES`` (default 15) from the env."""
    minutes = int(os.environ.get("SANDBOX_IDLE_TEARDOWN_MINUTES", "15"))
    return minutes * 60


def boot_timeout_seconds() -> int:
    """How long to wait for a sandbox to report healthy. Default 90."""
    return int(os.environ.get("SANDBOX_BOOT_TIMEOUT_S", "90"))


def state_file_path() -> Path:
    """Where to persist sandbox state across unified_api restarts."""
    override = os.environ.get("SANDBOX_STATE_FILE")
    if override:
        return Path(override)
    cache = os.environ.get("AGENT_CACHE", "/tmp/agents")
    return Path(cache) / "agent_sandbox" / "state.json"
