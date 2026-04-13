"""Accessibility audit top-level graph — 5-phase workflow.

Topology::

    intake → discovery_subgraph → verification_subgraph → report_packaging → quality_review

Discovery and verification use fan-out/fan-in subgraphs for
parallel multi-lane execution.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential

from .discovery_subgraph import build_discovery_subgraph
from .verification_subgraph import build_verification_subgraph


def build_audit_graph() -> Graph:
    """Build the full accessibility audit graph.

    Returns
    -------
    Graph
        Five-phase audit pipeline with parallel discovery and verification.
    """
    return build_sequential(
        stages=[
            ("intake", build_agent(
                name="audit_intake",
                system_prompt=(
                    "You are an accessibility audit intake specialist. Analyze the target "
                    "application, determine scope (pages, components, platforms), identify "
                    "applicable WCAG conformance level, and create the audit plan. "
                    "Return structured JSON with audit_plan."
                ),
                description="Creates accessibility audit plan",
            )),
            ("discovery", build_discovery_subgraph()),
            ("verification", build_verification_subgraph()),
            ("report_packaging", build_agent(
                name="report_packager",
                system_prompt=(
                    "You are an accessibility report packager. Compile all discovery and "
                    "verification findings into a comprehensive VPAT/accessibility report. "
                    "Include executive summary, conformance level assessment, prioritized "
                    "findings, and remediation roadmap. Return structured JSON."
                ),
                description="Packages findings into accessibility report",
            )),
            ("quality_review", build_agent(
                name="quality_reviewer",
                system_prompt=(
                    "You are an accessibility quality reviewer. Review the complete audit "
                    "report for accuracy, completeness, and actionability. Verify all WCAG "
                    "criteria were evaluated and findings are properly documented. "
                    "Return JSON with review_status and any corrections."
                ),
                description="Quality-checks the final audit report",
            )),
        ],
        graph_id="accessibility_audit",
        execution_timeout=900.0,
        node_timeout=180.0,
    )
