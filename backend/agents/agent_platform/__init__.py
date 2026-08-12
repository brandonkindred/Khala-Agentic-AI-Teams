"""In-process agent platform: registry, console, sandbox, and Studio authoring.

This package is the cohesive backend for discover / author / run / sandbox
agents. Members:

* ``agent_platform.registry`` — manifest catalog (not present yet; still
  ``agent_registry``)
* ``agent_platform.console`` — runs / saved-inputs / diff (not present yet;
  still ``agent_console``)
* ``agent_platform.sandbox`` — ephemeral sandbox runner (not present yet;
  still under ``agent_team_studio.agent_provisioning_team.sandbox``)
* ``agent_platform.studio`` — conversational single-agent authoring, including
  the ``/api/agent-studio`` HTTP router

Docker/env provisioning (``agent_team_studio.agent_provisioning_team`` minus
sandbox) is not a member. Domain apps (``agentic_team_provisioning``,
``user_agent_founder``) consume the platform; they are not members.

This module re-exports nothing. Import the subsystem packages directly
(``from agent_platform.studio import router``).
"""
