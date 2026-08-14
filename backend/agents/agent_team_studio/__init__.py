"""Agent Team Studio: provisioning and testing for Khala agents and teams.

Consolidates ``agentic_team_provisioning``, ``agent_provisioning_team``, and
``user_agent_founder`` under one namespace. Agent Studio authoring now lives
in ``agent_platform.studio``. Each remaining subpackage keeps its own API app,
Temporal workers, and Postgres schema — this package only groups them on disk.
"""
