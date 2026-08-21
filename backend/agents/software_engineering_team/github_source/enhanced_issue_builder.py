"""Enhanced GitHub issue builder for out-of-scope code review findings.

Renders a proposal (from :func:`issue_proposals.proposal_from_findings`) into a
rich GitHub issue body with:
- Description
- Label recommendation
- Fibonacci complexity score (based on conceptual complexity, anticipated LOC,
  blast radius, and solution complexity)
- Acceptance criteria
- Out-of-scope items (the finding locations)
- Desired outcome
- Dependencies

The complexity score is computed heuristically from the proposal's content — no
LLM call is required — using the same Fibonacci scale as the existing issue
grooming subsystem (:data:`issue_scoring.FIBONACCI_COMPLEXITY_VALUES`).

Used by the out-of-scope issue filing flow when a user approves proposals from
the Coding Team Issues tab, and by the automated queue agent that checks for
duplicates before filing.
"""

from __future__ import annotations

from typing import Any

from .issue_scoring import FIBONACCI_COMPLEXITY_VALUES

# The Fibonacci scale this module uses — reuse the canonical set from issue_scoring.
_FIBONACCI = FIBONACCI_COMPLEXITY_VALUES

# Severity → base conceptual complexity score mapping. Higher severity implies
# a harder problem to reason about.
_SEVERITY_CONCEPTUAL: dict[str, int] = {
    "critical": 8,
    "high": 5,
    "medium": 3,
    "low": 2,
    "info": 1,
}

# Category → base blast radius score. Categories that tend to affect many files
# or cross-cutting concerns score higher.
_CATEGORY_BLAST_RADIUS: dict[str, int] = {
    "architecture": 8,
    "integration": 8,
    "security": 5,
    "side-effects": 5,
    "logic": 3,
    "structure": 3,
    "testing": 3,
    "refactor": 3,
    "maintainability": 2,
    "spec-compliance": 2,
    "standards": 2,
    "naming": 1,
    "documentation": 1,
    "general": 2,
}

# Number of locations → LOC multiplier hint. More locations implies more code to
# change.
_LOC_THRESHOLDS = [
    (1, 2),
    (3, 3),
    (5, 5),
    (8, 8),
]


def _nearest_fibonacci(n: int) -> int:
    """Snap ``n`` to the nearest value in ``_FIBONACCI``, rounding ties up.

    Postconditions:
        - Returns the closest member of :data:`_FIBONACCI` to ``n``. An exact tie
          is resolved in favor of the larger value.
    """
    if n <= _FIBONACCI[0]:
        return _FIBONACCI[0]
    if n >= _FIBONACCI[-1]:
        return _FIBONACCI[-1]
    best = _FIBONACCI[0]
    best_dist = abs(n - best)
    for f in _FIBONACCI[1:]:
        dist = abs(n - f)
        if dist < best_dist or (dist == best_dist and f > best):
            best = f
            best_dist = dist
    return best


def compute_complexity_score(proposal: dict[str, Any]) -> dict[str, Any]:
    """Compute a Fibonacci complexity breakdown for a proposal.

    The score is based on four dimensions:
    - Conceptual complexity: derived from severity and category.
    - Anticipated lines of code: derived from the number of affected locations.
    - Blast radius: derived from the category and number of distinct files affected.
    - Solution complexity: derived from the description length, presence of
      multiple locations, and category.

    Preconditions:
        - ``proposal`` is a dict produced by :func:`proposal_from_findings`.
    Postconditions:
        - Returns a dict with keys ``conceptual``, ``anticipated_loc``,
          ``blast_radius``, ``solution_complexity``, ``aggregate``, and
          per-dimension ``*_rationale`` strings. Each score field is a member
          of :data:`_FIBONACCI`. ``aggregate`` is the nearest Fibonacci to the
          max of the four dimension scores. Pure — no side effects, no I/O.
    """
    severity = str(proposal.get("severity") or "info").lower()
    category = str(proposal.get("category") or "general").lower()
    locations = proposal.get("locations") or []
    num_locations = max(len(locations), 1)
    description = str(proposal.get("description") or "")
    suggestion = str(proposal.get("suggestion") or "")

    # --- Conceptual complexity ---
    base_conceptual = _SEVERITY_CONCEPTUAL.get(severity, 2)
    # Bump for cross-cutting categories
    if category in ("architecture", "integration", "security"):
        base_conceptual = max(base_conceptual, 5)
    conceptual = _nearest_fibonacci(base_conceptual)
    conceptual_rationale = (
        f"Severity '{severity}' with category '{category}' "
        f"indicates {'high' if conceptual >= 5 else 'moderate' if conceptual >= 3 else 'low'} "
        f"conceptual complexity."
    )

    # --- Anticipated LOC ---
    loc_score = 2  # default
    for threshold, score in _LOC_THRESHOLDS:
        if num_locations >= threshold:
            loc_score = score
    # Longer descriptions often indicate more code changes needed
    if len(description) > 300:
        loc_score = max(loc_score, 3)
    if len(description) > 600:
        loc_score = max(loc_score, 5)
    loc_score = _nearest_fibonacci(loc_score)
    loc_rationale = (
        f"{num_locations} location(s) affected; "
        f"description length suggests {'substantial' if loc_score >= 5 else 'moderate' if loc_score >= 3 else 'minor'} "
        f"code changes."
    )

    # --- Blast radius ---
    base_blast = _CATEGORY_BLAST_RADIUS.get(category, 2)
    # Multiple distinct files increase blast radius
    distinct_files = len({str(loc.get("file_path") or "") for loc in locations if loc.get("file_path")})
    if distinct_files > 3:
        base_blast = max(base_blast, 5)
    elif distinct_files > 1:
        base_blast = max(base_blast, 3)
    blast_radius = _nearest_fibonacci(base_blast)
    blast_rationale = (
        f"Category '{category}' across {distinct_files} distinct file(s) "
        f"gives a {'wide' if blast_radius >= 5 else 'moderate' if blast_radius >= 3 else 'narrow'} "
        f"blast radius."
    )

    # --- Solution complexity ---
    # Based on description + suggestion length (proxy for how involved the fix is)
    # and whether the issue spans multiple locations
    combined_len = len(description) + len(suggestion)
    if combined_len > 800 or num_locations > 5:
        base_solution = 8
    elif combined_len > 400 or num_locations > 3:
        base_solution = 5
    elif combined_len > 200 or num_locations > 1:
        base_solution = 3
    else:
        base_solution = 2
    # Cross-cutting categories imply harder solutions
    if category in ("architecture", "security", "integration"):
        base_solution = max(base_solution, 5)
    solution_complexity = _nearest_fibonacci(base_solution)
    solution_rationale = (
        f"{'Complex' if solution_complexity >= 5 else 'Moderate' if solution_complexity >= 3 else 'Simple'} "
        f"solution anticipated based on finding detail and scope."
    )

    # --- Aggregate ---
    aggregate = _nearest_fibonacci(
        max(conceptual, loc_score, blast_radius, solution_complexity)
    )

    return {
        "conceptual": conceptual,
        "conceptual_rationale": conceptual_rationale,
        "anticipated_loc": loc_score,
        "anticipated_loc_rationale": loc_rationale,
        "blast_radius": blast_radius,
        "blast_radius_rationale": blast_rationale,
        "solution_complexity": solution_complexity,
        "solution_complexity_rationale": solution_rationale,
        "aggregate": aggregate,
    }


def _derive_label(proposal: dict[str, Any]) -> str:
    """Derive a suggested GitHub issue label from the proposal's category/severity.

    Postconditions:
        - Returns a short label string suitable for a GitHub issue label.
          Pure — no side effects.
    """
    category = str(proposal.get("category") or "general").lower()
    severity = str(proposal.get("severity") or "info").lower()

    # Map categories to conventional label names
    label_map: dict[str, str] = {
        "logic": "bug",
        "security": "security",
        "architecture": "architecture",
        "integration": "integration",
        "testing": "testing",
        "refactor": "refactor",
        "maintainability": "maintenance",
        "side-effects": "bug",
        "standards": "standards",
        "spec-compliance": "spec-compliance",
        "naming": "code-quality",
        "structure": "code-quality",
        "documentation": "documentation",
        "general": "code-quality",
    }
    label = label_map.get(category, "code-quality")

    # Prefix with severity for critical/high
    if severity in ("critical", "high"):
        label = f"priority:{severity}"

    return label


def _derive_acceptance_criteria(proposal: dict[str, Any]) -> list[str]:
    """Derive acceptance criteria from the proposal content.

    Postconditions:
        - Returns a list of acceptance criteria strings. Always non-empty.
          Pure — no side effects.
    """
    criteria: list[str] = []
    description = str(proposal.get("description") or "")
    suggestion = str(proposal.get("suggestion") or "")
    locations = proposal.get("locations") or []
    category = str(proposal.get("category") or "general").lower()

    # Primary criterion: the issue described is resolved
    if description:
        criteria.append(f"The issue described is resolved: {description[:120]}{'...' if len(description) > 120 else ''}")

    # If suggestion exists, it becomes a criterion
    if suggestion:
        criteria.append(f"Fix implemented: {suggestion[:120]}{'...' if len(suggestion) > 120 else ''}")

    # Location-based criteria
    if len(locations) > 1:
        criteria.append(f"All {len(locations)} affected locations are addressed")
    elif locations:
        loc = locations[0]
        fp = str(loc.get("file_path") or "")
        if fp:
            criteria.append(f"Fix applied in `{fp}`")

    # Category-specific criteria
    if category == "testing":
        criteria.append("Relevant tests are added or updated")
    elif category == "security":
        criteria.append("Security vulnerability is eliminated and cannot regress")
    elif category in ("architecture", "integration"):
        criteria.append("Integration/contract tests pass")

    # Fallback
    if not criteria:
        criteria.append("The identified code quality issue is resolved")

    criteria.append("No regressions introduced")
    return criteria


def _derive_desired_outcome(proposal: dict[str, Any]) -> str:
    """Derive the desired outcome from the proposal.

    Postconditions:
        - Returns a concise desired-outcome statement. Pure — no side effects.
    """
    description = str(proposal.get("description") or "")
    suggestion = str(proposal.get("suggestion") or "")
    category = str(proposal.get("category") or "general").lower()

    if suggestion:
        return f"The codebase no longer exhibits this issue. Specifically: {suggestion[:200]}{'...' if len(suggestion) > 200 else ''}"
    if description:
        return f"The identified problem is resolved: {description[:200]}{'...' if len(description) > 200 else ''}"
    return f"The {category} issue is resolved and the affected code meets project standards."


def _derive_dependencies(proposal: dict[str, Any]) -> list[str]:
    """Derive potential dependencies from the proposal.

    Postconditions:
        - Returns a list of dependency descriptions (may be empty).
          Pure — no side effects.
    """
    deps: list[str] = []
    category = str(proposal.get("category") or "general").lower()
    locations = proposal.get("locations") or []
    distinct_files = {str(loc.get("file_path") or "") for loc in locations if loc.get("file_path")}

    if category in ("architecture", "integration"):
        deps.append("Dependent modules may need coordinated updates")
    if category == "testing":
        deps.append("Test infrastructure and fixtures may need updates")
    if len(distinct_files) > 3:
        deps.append(f"Changes span {len(distinct_files)} files — coordinate to avoid conflicts")

    return deps


def build_enhanced_issue_from_proposal(
    proposal: dict[str, Any], *, pr_number: int, pr_url: str
) -> tuple[str, str, str]:
    """Render a proposal as an enhanced ``(title, body, label)`` for a new GitHub issue.

    The body includes: description, Fibonacci complexity score breakdown,
    acceptance criteria, out-of-scope items (locations), desired outcome,
    and dependencies.

    Preconditions:
        - ``proposal`` is a dict produced by :func:`proposal_from_findings`.
        - ``pr_number``/``pr_url`` identify the originating PR.
    Postconditions:
        - Returns ``(title, body, label)``. ``title`` is the concise headline;
          ``body`` is rich markdown; ``label`` is a suggested GitHub label.
          Pure — no side effects, no I/O.
    """
    from .issue_proposals import _proposal_title

    title = _proposal_title(proposal)
    severity = str(proposal.get("severity") or "info").lower()
    category = str(proposal.get("category") or "general")
    description = str(proposal.get("description") or "").strip()
    suggestion = str(proposal.get("suggestion") or "").strip()
    locations = proposal.get("locations") or []
    label = _derive_label(proposal)

    # Compute complexity score
    complexity = compute_complexity_score(proposal)

    # Build the body
    lines: list[str] = []

    # --- Provenance ---
    lines.append(
        f"> Identified during code review of PR #{pr_number} ({pr_url}) as a "
        f"**pre-existing issue** in code the pull request did not modify."
    )
    lines.append("")

    # --- Description ---
    lines.append("## Description")
    lines.append("")
    lines.append(description or "_No description provided._")
    lines.append("")

    # --- Label ---
    lines.append(f"**Label:** `{label}`")
    lines.append("")

    # --- Fibonacci Complexity Score ---
    lines.append("## Complexity Score")
    lines.append("")
    lines.append(f"| Dimension | Score | Rationale |")
    lines.append(f"|-----------|-------|-----------|")
    lines.append(
        f"| Conceptual complexity | **{complexity['conceptual']}** | "
        f"{complexity['conceptual_rationale']} |"
    )
    lines.append(
        f"| Anticipated lines of code | **{complexity['anticipated_loc']}** | "
        f"{complexity['anticipated_loc_rationale']} |"
    )
    lines.append(
        f"| Blast radius | **{complexity['blast_radius']}** | "
        f"{complexity['blast_radius_rationale']} |"
    )
    lines.append(
        f"| Solution complexity | **{complexity['solution_complexity']}** | "
        f"{complexity['solution_complexity_rationale']} |"
    )
    lines.append(
        f"| **Aggregate** | **{complexity['aggregate']}** | "
        f"Max of dimension scores, snapped to Fibonacci |"
    )
    lines.append("")

    # --- Acceptance Criteria ---
    criteria = _derive_acceptance_criteria(proposal)
    lines.append("## Acceptance Criteria")
    lines.append("")
    for criterion in criteria:
        lines.append(f"- [ ] {criterion}")
    lines.append("")

    # --- Out of Scope Items (locations) ---
    lines.append("## Out of Scope Items")
    lines.append("")
    if len(locations) > 1:
        for loc in locations:
            fp = str(loc.get("file_path") or "unknown")
            line_num = loc.get("line")
            loc_desc = str(loc.get("description") or "").strip()
            loc_text = f"`{fp}:{line_num}`" if isinstance(line_num, int) and line_num > 0 else f"`{fp}`"
            lines.append(f"- {loc_text} — {loc_desc or '_No description._'}")
    else:
        fp = str(proposal.get("file_path") or "")
        line_num = proposal.get("line")
        if fp:
            loc_text = f"`{fp}:{line_num}`" if isinstance(line_num, int) and line_num > 0 else f"`{fp}`"
            lines.append(f"- {loc_text}")
        else:
            lines.append("- _No specific location identified._")
    lines.append("")

    # --- Desired Outcome ---
    desired_outcome = _derive_desired_outcome(proposal)
    lines.append("## Desired Outcome")
    lines.append("")
    lines.append(desired_outcome)
    lines.append("")

    # --- Dependencies ---
    dependencies = _derive_dependencies(proposal)
    lines.append("## Dependencies")
    lines.append("")
    if dependencies:
        for dep in dependencies:
            lines.append(f"- {dep}")
    else:
        lines.append("- None identified")
    lines.append("")

    # --- Suggested Fix (if available) ---
    if suggestion:
        lines.append("## Suggested Fix")
        lines.append("")
        lines.append(suggestion)
        lines.append("")

    # --- Metadata ---
    lines.append("---")
    lines.append(f"*Severity: {severity} | Category: {category} | Complexity: {complexity['aggregate']}*")

    return title, "\n".join(lines), label
