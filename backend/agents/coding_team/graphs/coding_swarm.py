"""Coding team swarm — replaces the hand-rolled CodingTeamSwarm while loop.

The Tech Lead reasons about task assignment, implementation, and quality
gates, handing off to specialist implementers and quality gate runners
as needed. This is a textbook Swarm use case: the coordinator agent
reasons about who should act next.

Agents:
    tech_lead_assigner (entry) ←→ implementer ←→ quality_gate_runner ←→ reviewer_merger

Replaces the 500-round while loop in CodingTeamSwarm with max_handoffs=50
(sufficient for complex multi-task workflows).
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_coding_swarm(
    *,
    max_handoffs: int = 50,
) -> Swarm:
    """Build the coding team swarm.

    Parameters
    ----------
    max_handoffs:
        Maximum number of handoffs between agents. Higher values support
        more complex multi-task workflows.
    """
    tech_lead = build_agent(
        name="tech_lead_assigner",
        system_prompt=(
            "You are a Tech Lead coordinating a coding team. Your responsibilities:\n"
            "1. Analyze the task graph and determine which tasks are ready for implementation\n"
            "2. Assign tasks to available implementers with clear requirements\n"
            "3. Track progress and handle dependencies between tasks\n"
            "4. After implementation, hand off to quality_gate_runner for verification\n"
            "5. After quality gates pass, hand off to reviewer_merger for final review\n\n"
            "When assigning tasks, provide: task description, acceptance criteria, "
            "relevant context from completed tasks, and any dependency outputs.\n"
            "Hand off to implementer when tasks are ready."
        ),
        agent_key="tech_lead",
        description="Coordinates task assignment and team progress",
    )

    implementer = build_agent(
        name="implementer",
        system_prompt=(
            "You are a Senior Software Engineer. Implement the assigned task:\n"
            "1. Analyze the requirements and acceptance criteria\n"
            "2. Write clean, well-structured code following project conventions\n"
            "3. Include appropriate tests\n"
            "4. When implementation is complete, hand off to quality_gate_runner\n"
            "5. If quality gates fail, apply fixes and re-submit\n\n"
            "Return JSON with: files (dict of path→content), summary, and "
            "suggested_commit_message."
        ),
        agent_key="coding_team",
        description="Implements coding tasks with tests",
    )

    quality_gate = build_agent(
        name="quality_gate_runner",
        system_prompt=(
            "You are a Quality Gate Runner. Verify the implementation:\n"
            "1. Check code compiles/lints successfully\n"
            "2. Verify tests pass\n"
            "3. Run code review checks\n"
            "4. If all gates pass, hand off to reviewer_merger\n"
            "5. If gates fail, hand back to implementer with specific failure details\n\n"
            "Return JSON with: gate_results (array), all_passed (boolean), "
            "failure_details (if any)."
        ),
        agent_key="coding_team",
        description="Runs quality gates on implementations",
    )

    reviewer_merger = build_agent(
        name="reviewer_merger",
        system_prompt=(
            "You are a Code Reviewer and Merger. Final review before merge:\n"
            "1. Review the implementation for correctness and style\n"
            "2. Verify it meets the original task requirements\n"
            "3. If approved, prepare the merge commit\n"
            "4. If issues found, hand back to implementer for revision\n"
            "5. After merge, hand back to tech_lead_assigner for next task\n\n"
            "Return JSON with: approved (boolean), review_notes, merge_ready."
        ),
        agent_key="coding_team",
        description="Reviews and merges completed implementations",
    )

    return Swarm(
        nodes=[tech_lead, implementer, quality_gate, reviewer_merger],
        entry_point=tech_lead,
        max_handoffs=max_handoffs,
        execution_timeout=600.0,
    )
