"""SOC2 audit graph — fan-out/fan-in topology.

Five TSC specialist agents run in parallel, then a report writer
synthesizes all findings into a compliance report or next-steps document.

Topology::

    security_tsc ──────────────┐
    availability_tsc ──────────┤
    processing_integrity_tsc ──┼──▶ report_writer
    confidentiality_tsc ───────┤
    privacy_tsc ───────────────┘
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_fan_out_fan_in

from ..agents import (
    make_availability_tsc_agent,
    make_confidentiality_tsc_agent,
    make_privacy_tsc_agent,
    make_processing_integrity_tsc_agent,
    make_report_writer_agent,
    make_security_tsc_agent,
)


def build_audit_graph() -> Graph:
    """Build the SOC2 audit fan-out/fan-in graph.

    Returns
    -------
    Graph
        Five parallel TSC auditors feeding a single report writer.
    """
    return build_fan_out_fan_in(
        agents=[
            ("security_tsc", make_security_tsc_agent()),
            ("availability_tsc", make_availability_tsc_agent()),
            ("processing_integrity_tsc", make_processing_integrity_tsc_agent()),
            ("confidentiality_tsc", make_confidentiality_tsc_agent()),
            ("privacy_tsc", make_privacy_tsc_agent()),
        ],
        compositor=("report_writer", make_report_writer_agent()),
        graph_id="soc2_audit",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
