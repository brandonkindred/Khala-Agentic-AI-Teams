"""In-process agent platform: registry, console, sandbox, and studio.

This package is the cohesive backend for discover / author / run / sandbox
agents. Subpackages are imported with a dotted prefix
(``agent_platform.sandbox``, later ``agent_platform.registry`` /
``agent_platform.console`` / ``agent_platform.studio``). This module re-exports
nothing — callers import from the subpackage that owns the symbol.

Boundary:
    * In: sandbox lifecycle (acquire / warm / teardown / reaper) and its
      Temporal wiring.
    * Out: Docker/env provisioning infra under
      ``agent_team_studio.agent_provisioning_team`` (tool agents, phases,
      orchestrator, provisioning Temporal). Domain apps that merely consume
      the platform (agentic compose, persona runner) stay consumers.

Preconditions:
    * ``backend/agents`` is on ``PYTHONPATH``.
Postconditions:
    * Importing ``agent_platform`` has no side effects (no worker boot, no
      I/O). Subpackages are loaded only when callers import them.
"""
