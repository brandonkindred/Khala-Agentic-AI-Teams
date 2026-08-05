"""Agent Team Studio: authoring, provisioning, and testing for Khala agents and teams.

Consolidates the previously top-level packages ``agent_studio``,
``agentic_team_provisioning``, ``agent_provisioning_team``, and
``user_agent_founder`` under one namespace. Each subpackage keeps its own
API app, Temporal workers, and Postgres schema — this package only groups
them on disk.
"""
