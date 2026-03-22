"""
Tool: qa.cluster_patterns

Dedupe and cluster findings into systemic patterns.
"""

import uuid
from collections import defaultdict
from typing import List, Literal

from pydantic import BaseModel, Field

from ...models import Finding, PatternCluster, Scope, Severity


class ClusterPatternsInput(BaseModel):
    """Input for clustering findings into patterns."""

    audit_id: str = Field(..., description="Audit identifier")
    findings: List[Finding] = Field(
        default_factory=list, description="Findings to cluster"
    )
    cluster_by: List[Literal["component", "issue_type", "wcag_sc"]] = Field(
        default_factory=lambda: ["component", "issue_type"],
        description="Dimensions to cluster by",
    )
    min_cluster_size: int = Field(
        default=2, description="Minimum findings for a cluster"
    )


class ClusterPatternsOutput(BaseModel):
    """Output from pattern clustering."""

    patterns: List[PatternCluster] = Field(default_factory=list)
    total_patterns: int = Field(default=0)
    systemic_patterns: int = Field(
        default=0, description="Patterns affecting 3+ areas"
    )
    findings_clustered: int = Field(default=0)
    unclustered_findings: List[str] = Field(
        default_factory=list, description="Finding IDs not in any cluster"
    )


async def cluster_patterns(
    input_data: ClusterPatternsInput,
) -> ClusterPatternsOutput:
    """
    Cluster findings into systemic patterns.

    Patterns enable:
    - Fixing root causes instead of individual symptoms
    - Prioritizing high-impact systemic issues
    - Tracking similar issues as one unit
    - Design system improvements

    Deduplicate by pattern, not by page count.

    Used by QA & Consistency Reviewer (QCR).
    """
    findings = input_data.findings
    patterns = []
    clustered_ids = set()

    # Cluster by issue type first
    if "issue_type" in input_data.cluster_by:
        by_issue_type = defaultdict(list)
        for f in findings:
            by_issue_type[f.issue_type].append(f)

        for issue_type, type_findings in by_issue_type.items():
            if len(type_findings) >= input_data.min_cluster_size:
                # Further cluster by component if requested
                if "component" in input_data.cluster_by:
                    by_component = defaultdict(list)
                    for f in type_findings:
                        comp = f.component_id or "unknown"
                        by_component[comp].append(f)

                    for comp, comp_findings in by_component.items():
                        if len(comp_findings) >= input_data.min_cluster_size:
                            pattern_id = f"pattern_{uuid.uuid4().hex[:8]}"
                            linked_ids = [f.id for f in comp_findings]
                            clustered_ids.update(linked_ids)

                            # Determine severity (highest in cluster)
                            severities = [f.severity for f in comp_findings]
                            max_severity = max(severities, key=lambda s: [
                                Severity.CRITICAL, Severity.HIGH,
                                Severity.MEDIUM, Severity.LOW
                            ].index(s))

                            # Determine scope
                            if len(comp_findings) >= 5:
                                scope = Scope.SYSTEMIC
                            elif len(comp_findings) >= 3:
                                scope = Scope.MULTI_AREA
                            else:
                                scope = Scope.LOCALIZED

                            patterns.append(
                                PatternCluster(
                                    pattern_id=pattern_id,
                                    name=f"{issue_type.value} in {comp}",
                                    description=f"Pattern of {len(comp_findings)} {issue_type.value} issues in component {comp}",
                                    linked_finding_ids=linked_ids,
                                    severity=max_severity,
                                    scope=scope,
                                    issue_types=[issue_type],
                                    wcag_scs=list(set(
                                        m.sc for f in comp_findings for m in f.wcag_mappings
                                    )),
                                    component_ids=[comp] if comp != "unknown" else [],
                                    fix_priority=len(comp_findings),  # More findings = higher priority
                                )
                            )
                else:
                    # Cluster by issue type only
                    pattern_id = f"pattern_{uuid.uuid4().hex[:8]}"
                    linked_ids = [f.id for f in type_findings]
                    clustered_ids.update(linked_ids)

                    severities = [f.severity for f in type_findings]
                    max_severity = max(severities, key=lambda s: [
                        Severity.CRITICAL, Severity.HIGH,
                        Severity.MEDIUM, Severity.LOW
                    ].index(s))

                    if len(type_findings) >= 5:
                        scope = Scope.SYSTEMIC
                    elif len(type_findings) >= 3:
                        scope = Scope.MULTI_AREA
                    else:
                        scope = Scope.LOCALIZED

                    patterns.append(
                        PatternCluster(
                            pattern_id=pattern_id,
                            name=f"{issue_type.value} pattern",
                            description=f"Pattern of {len(type_findings)} {issue_type.value} issues",
                            linked_finding_ids=linked_ids,
                            severity=max_severity,
                            scope=scope,
                            issue_types=[issue_type],
                            wcag_scs=list(set(
                                m.sc for f in type_findings for m in f.wcag_mappings
                            )),
                            component_ids=[],
                            fix_priority=len(type_findings),
                        )
                    )

    # Identify unclustered findings
    unclustered = [f.id for f in findings if f.id not in clustered_ids]

    # Sort patterns by fix priority (descending)
    patterns.sort(key=lambda p: p.fix_priority, reverse=True)

    # Renumber priorities
    for i, p in enumerate(patterns):
        p.fix_priority = i + 1

    systemic = sum(1 for p in patterns if p.scope == Scope.SYSTEMIC)

    return ClusterPatternsOutput(
        patterns=patterns,
        total_patterns=len(patterns),
        systemic_patterns=systemic,
        findings_clustered=len(clustered_ids),
        unclustered_findings=unclustered,
    )
