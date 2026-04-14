"""DevOps Phase 4 — Validation fan-out graph with debug-patch cycle.

Six to eight independent validation checks run in parallel, replacing
sequential execution. Expected 50-70% wall-clock reduction.

Topology::

    iac_validator ──────────┐
    policy_validator ───────┤
    cicd_validator ─────────┼──▶ validation_gate ──▶ debug_patch_cycle
    dry_run_validator ──────┤
    security_validator ─────┤
    cost_validator ─────────┘

The validation_gate aggregates results. If critical failures exist,
the debug_patch_cycle (bounded to MAX_INFRA_FIX_ITERATIONS=3) runs
infra_debug_agent → infra_patch_agent → re-validate.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent


def build_phase4_validation_graph() -> Graph:
    """Build the Phase 4 parallel validation graph."""
    builder = GraphBuilder()
    builder.set_graph_id("devops_phase4_validation")
    builder.set_execution_timeout(600.0)
    builder.set_node_timeout(120.0)

    validators = {
        "iac_validator": (
            "You validate Infrastructure-as-Code artifacts for syntax, "
            "best practices, and security compliance."
        ),
        "policy_validator": (
            "You validate infrastructure against organizational policies: "
            "naming conventions, tagging, cost controls, and compliance."
        ),
        "cicd_validator": (
            "You validate CI/CD pipeline configuration for correctness, "
            "security (secrets handling), and efficiency."
        ),
        "dry_run_validator": (
            "You perform dry-run validation of deployments to catch "
            "runtime errors before actual deployment."
        ),
        "security_validator": (
            "You perform security validation: IAM policies, network rules, "
            "encryption settings, and vulnerability scanning."
        ),
        "cost_validator": (
            "You estimate and validate infrastructure costs against budget "
            "constraints and flag unexpectedly expensive resources."
        ),
    }

    validator_nodes = []
    for name, desc in validators.items():
        node = builder.add_node(
            build_agent(
                name=name,
                system_prompt=(
                    f"{desc}\n\nReturn JSON with: passed (bool), findings array, "
                    f"severity per finding, and remediation suggestions."
                ),
                description=f"Validates: {name.replace('_', ' ')}",
            ),
            node_id=name,
        )
        builder.set_entry_point(name)
        validator_nodes.append(node)

    # Validation gate aggregates all results
    gate = builder.add_node(
        build_agent(
            name="validation_gate",
            system_prompt=(
                "You are the validation gate aggregator. Combine all validation "
                "results, identify critical failures, and determine if the changes "
                "are safe to deploy. Return JSON with: all_passed (bool), "
                "critical_failures array, summary."
            ),
            description="Aggregates validation results",
        ),
        node_id="validation_gate",
    )

    for vn in validator_nodes:
        builder.add_edge(vn, gate)

    return builder.build()
