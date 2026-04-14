"""DevOps Phase 2 — Change Design fan-out graph.

Three independent design agents run in parallel despite no cross-
dependencies, replacing the sequential execution pattern.

Topology::

    iac_designer ──────────┐
    cicd_designer ─────────┼──▶ design_aggregator
    deployment_designer ───┘

Expected: ~3x latency reduction vs sequential execution.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_fan_out_fan_in


def build_phase2_design_graph() -> Graph:
    """Build the Phase 2 three-way fan-out design graph."""
    return build_fan_out_fan_in(
        agents=[
            ("iac_designer", build_agent(
                name="iac_designer",
                system_prompt=(
                    "You are an Infrastructure-as-Code specialist. Design the IaC artifacts "
                    "(Terraform, CloudFormation, Pulumi) for the requested infrastructure "
                    "changes. Return structured JSON with iac_content and design_decisions."
                ),
                description="Designs Infrastructure-as-Code artifacts",
            )),
            ("cicd_designer", build_agent(
                name="cicd_designer",
                system_prompt=(
                    "You are a CI/CD pipeline specialist. Design the CI/CD pipeline "
                    "configuration (GitHub Actions, GitLab CI, etc.) for the project. "
                    "Return structured JSON with pipeline_yaml and design_decisions."
                ),
                description="Designs CI/CD pipeline configuration",
            )),
            ("deployment_designer", build_agent(
                name="deployment_designer",
                system_prompt=(
                    "You are a deployment strategy specialist. Design the deployment "
                    "configuration (Dockerfile, docker-compose, Kubernetes manifests) "
                    "for the project. Return structured JSON with deployment artifacts."
                ),
                description="Designs deployment configuration",
            )),
        ],
        compositor=("design_aggregator", build_agent(
            name="design_aggregator",
            system_prompt=(
                "You are a DevOps architect. Aggregate the three design outputs (IaC, CI/CD, "
                "deployment) into a coherent change plan. Resolve any conflicts between "
                "the designs and produce a unified artifact set. Return structured JSON."
            ),
            description="Aggregates all design artifacts",
        )),
        graph_id="devops_phase2_design",
        execution_timeout=300.0,
        node_timeout=120.0,
    )
