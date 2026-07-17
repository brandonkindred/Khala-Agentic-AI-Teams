"""Architect agents: ArchitectureExpertAgent plus the Enterprise Orchestrator.

Makes ``software_engineering_team.architect_agents.architecture_expert`` a
proper dotted import path. This module must stay import-light: ``main``,
``integration``, and ``agents`` are subprocess-only entry points with their
own ``sys.path`` bootstrap and heavy optional dependencies (strands tools,
boto3/bedrock), so importing them here would break the cheap-import contract
that the SE orchestrator and the coding-engine provider rely on.

Invariants:
    - Importing this package performs no side effects beyond defining the
      package namespace (no sub-module imports, no path mutation).
"""
