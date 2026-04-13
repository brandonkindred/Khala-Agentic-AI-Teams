"""Accessibility audit discovery subgraph — two-lane parallel scan.

Topology::

    web_accessibility_scanner ──┬──▶ discovery_merger
    mobile_accessibility_scanner ┘

WAS and MAS run in parallel, then their findings are merged.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_fan_out_fan_in


def build_discovery_subgraph() -> Graph:
    """Build the discovery phase fan-out/fan-in graph."""
    return build_fan_out_fan_in(
        agents=[
            ("web_scanner", build_agent(
                name="web_accessibility_scanner",
                system_prompt=(
                    "You are a web accessibility scanner (WAS). Analyze the web application "
                    "for WCAG 2.2 compliance issues across all conformance levels (A, AA, AAA). "
                    "Test automated checks, color contrast, keyboard navigation, screen reader "
                    "compatibility, and ARIA usage. Return structured JSON with findings."
                ),
                description="Scans web interfaces for WCAG compliance",
            )),
            ("mobile_scanner", build_agent(
                name="mobile_accessibility_scanner",
                system_prompt=(
                    "You are a mobile accessibility scanner (MAS). Analyze mobile app "
                    "interfaces for accessibility issues: touch target sizes, gesture "
                    "alternatives, screen reader support, dynamic content, and platform-specific "
                    "guidelines (iOS/Android). Return structured JSON with findings."
                ),
                description="Scans mobile interfaces for accessibility",
            )),
        ],
        compositor=("discovery_merger", build_agent(
            name="discovery_merger",
            system_prompt=(
                "You are an accessibility findings merger. Combine web and mobile scan "
                "results into a unified discovery report. Deduplicate overlapping issues, "
                "prioritize by severity and impact, and identify patterns. "
                "Return structured JSON with merged findings."
            ),
            description="Merges web and mobile scan results",
        )),
        graph_id="a11y_discovery",
        execution_timeout=300.0,
        node_timeout=120.0,
    )
