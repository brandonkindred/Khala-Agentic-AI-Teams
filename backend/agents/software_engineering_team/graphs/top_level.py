"""Software Engineering team top-level graph — 4-phase composition.

Topology::

    discovery → design → execution → integration

Each phase is a sub-graph or swarm:
- Discovery: Product analysis + planning (sequential)
- Design: Tech Lead task assignment + architecture (fan-out/fan-in)
- Execution: Backend + frontend parallel sub-graphs with review gates
- Integration: Resolution swarm (self-healing conflict resolution)

This graph composes the sub-team graphs into the overall SE pipeline,
replacing the 3480-line sequential orchestrator.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_se_top_level_graph() -> Graph:
    """Build the top-level Software Engineering pipeline graph.

    Returns
    -------
    Graph
        Four-phase SE pipeline: discovery → design → execution → integration.
    """
    return build_sequential(
        stages=[
            ("discovery", build_agent(
                name="se_discovery",
                system_prompt=(
                    "You are the SE team discovery coordinator. Parse the project spec, "
                    "run product requirements analysis, and produce a validated specification "
                    "with acceptance criteria. Coordinate with the planning system to produce "
                    "a task breakdown. Return JSON with: validated_spec, task_assignments, "
                    "architecture_document."
                ),
                description="Runs spec analysis and planning",
            )),
            ("design", build_agent(
                name="se_design",
                system_prompt=(
                    "You are the SE Tech Lead. Based on the discovery output:\n"
                    "1. Generate task assignments with execution order\n"
                    "2. Partition tasks: git_setup/devops (prefix), backend, frontend\n"
                    "3. Create architecture documentation\n"
                    "4. Set up parallel execution queues\n\n"
                    "Return JSON with: task_assignments, execution_order, architecture_doc."
                ),
                agent_key="tech_lead",
                description="Assigns tasks and designs architecture",
            )),
            ("execution", build_agent(
                name="se_execution",
                system_prompt=(
                    "You are the SE execution coordinator. Execute assigned tasks:\n"
                    "1. Run prefix queue (git setup, DevOps) sequentially\n"
                    "2. Process backend tasks through Backend Code V2 pipeline\n"
                    "3. Process frontend tasks through Frontend Code V2 pipeline\n"
                    "4. Each task goes through: plan → code → lint → build → review gates\n\n"
                    "Return JSON with: completed_tasks, failed_tasks, artifacts."
                ),
                agent_key="coding_team",
                description="Executes backend and frontend tasks",
            )),
            ("integration", build_agent(
                name="se_integration",
                system_prompt=(
                    "You are the SE integration coordinator. After execution:\n"
                    "1. Run integration checks (backend ↔ frontend contract alignment)\n"
                    "2. Trigger DevOps validation pipeline\n"
                    "3. Run security pass across all changes\n"
                    "4. Update documentation\n"
                    "5. Prepare merge\n\n"
                    "Return JSON with: integration_status, security_findings, merge_ready."
                ),
                agent_key="coding_team",
                description="Integration, security, and merge coordination",
            )),
        ],
        graph_id="se_top_level",
        execution_timeout=1800.0,
        node_timeout=600.0,
    )
