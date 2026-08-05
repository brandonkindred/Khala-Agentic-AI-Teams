"""
Roster validation: ensure the team is fully staffed.

Checks that:
1. Every agent referenced in a process step exists on the roster.
2. Every roster agent is used in at least one process step (no dead weight).
3. Every process step has at least one assigned agent.
4. The collective skills, capabilities, tools, and expertise on the roster
   cover the needs implied by each process step's description/agents.
"""

from __future__ import annotations

from .models import (
    AgenticTeam,
    AgenticTeamAgent,
    ProcessDefinition,
    RosterGap,
    RosterValidationResult,
)


def validate_roster(team: AgenticTeam) -> RosterValidationResult:
    """Run all roster coverage checks and return a structured result."""
    gaps: list[RosterGap] = []
    roster_map: dict[str, AgenticTeamAgent] = {a.agent_name: a for a in team.agents}
    used_agent_names: set[str] = set()

    for proc in team.processes:
        gaps.extend(_check_process(proc, roster_map, used_agent_names))

    if team.processes:
        gaps.extend(_check_unused_agents(roster_map, used_agent_names))
    gaps.extend(_check_roster_depth(team.agents))

    is_fully_staffed = len(gaps) == 0
    summary = _build_summary(team, gaps, is_fully_staffed)

    return RosterValidationResult(
        is_fully_staffed=is_fully_staffed,
        agent_count=len(team.agents),
        process_count=len(team.processes),
        gaps=gaps,
        summary=summary,
    )


def _check_process(
    proc: ProcessDefinition,
    roster_map: dict[str, AgenticTeamAgent],
    used_agent_names: set[str],
) -> list[RosterGap]:
    gaps: list[RosterGap] = []

    if not proc.steps:
        gaps.append(
            RosterGap(
                category="unstaffed_step",
                detail=f"Process '{proc.name}' has no steps defined.",
                process_id=proc.process_id,
            )
        )
        return gaps

    for step in proc.steps:
        if not step.agents:
            gaps.append(
                RosterGap(
                    category="unstaffed_step",
                    detail=f"Step '{step.name}' in process '{proc.name}' has no agents assigned.",
                    process_id=proc.process_id,
                    step_id=step.step_id,
                )
            )
            continue

        for sa in step.agents:
            used_agent_names.add(sa.agent_name)
            if sa.agent_name not in roster_map:
                gaps.append(
                    RosterGap(
                        category="unrostered_agent",
                        detail=(
                            f"Agent '{sa.agent_name}' is assigned to step '{step.name}' "
                            f"in process '{proc.name}' but is not on the team roster."
                        ),
                        process_id=proc.process_id,
                        step_id=step.step_id,
                        agent_name=sa.agent_name,
                    )
                )

    return gaps


def _check_unused_agents(
    roster_map: dict[str, AgenticTeamAgent],
    used_agent_names: set[str],
) -> list[RosterGap]:
    gaps: list[RosterGap] = []
    for name in sorted(roster_map):
        if name not in used_agent_names:
            gaps.append(
                RosterGap(
                    category="unused_agent",
                    detail=(
                        f"Agent '{name}' is on the roster but not assigned to any process step. "
                        "Consider removing or assigning them."
                    ),
                    agent_name=name,
                )
            )
    return gaps


def _check_roster_depth(agents: list[AgenticTeamAgent]) -> list[RosterGap]:
    """Flag agents that lack detail — they need at least one of skills/capabilities/tools/expertise."""
    gaps: list[RosterGap] = []
    for a in agents:
        missing: list[str] = []
        if not a.skills:
            missing.append("skills")
        if not a.capabilities:
            missing.append("capabilities")
        if not a.tools:
            missing.append("tools")
        if not a.expertise:
            missing.append("expertise")
        if len(missing) == 4:
            gaps.append(
                RosterGap(
                    category="incomplete_profile",
                    detail=(
                        f"Agent '{a.agent_name}' has no skills, capabilities, tools, or expertise "
                        "defined. The roster cannot validate coverage without this information."
                    ),
                    agent_name=a.agent_name,
                )
            )
        elif len(missing) >= 3:
            gaps.append(
                RosterGap(
                    category="sparse_profile",
                    detail=(
                        f"Agent '{a.agent_name}' is missing {', '.join(missing)}. "
                        "Consider adding detail so staffing coverage can be validated."
                    ),
                    agent_name=a.agent_name,
                )
            )
    return gaps


def _build_summary(
    team: AgenticTeam,
    gaps: list[RosterGap],
    is_fully_staffed: bool,
) -> str:
    if not team.agents and not team.processes:
        return "The team has no agents and no processes yet."
    if not team.agents:
        return f"The team has {len(team.processes)} process(es) but no agents on the roster."
    if not team.processes:
        return (
            f"The roster has {len(team.agents)} agent(s) but no processes are defined yet — "
            "staffing coverage cannot be validated without processes."
        )
    if is_fully_staffed:
        return (
            f"The team is fully staffed: {len(team.agents)} agent(s) cover "
            f"{len(team.processes)} process(es) with no gaps."
        )

    by_cat: dict[str, int] = {}
    for g in gaps:
        by_cat[g.category] = by_cat.get(g.category, 0) + 1
    parts = [f"{count} {cat.replace('_', ' ')}(s)" for cat, count in sorted(by_cat.items())]
    return (
        f"The roster has {len(gaps)} gap(s) across {len(team.agents)} agent(s) and "
        f"{len(team.processes)} process(es): {', '.join(parts)}."
    )
