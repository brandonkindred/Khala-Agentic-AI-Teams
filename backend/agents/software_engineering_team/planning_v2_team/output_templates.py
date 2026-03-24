"""
Template-based output parsing for planning_v2_team.

Avoids reliance on JSON so that model output can be parsed reliably across
different providers and models. Uses section-delimited text that can be
parsed with simple string/regex extraction; partial or truncated output
can still yield useful results.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Generous cap for artifact body text (planning V2 truncation fix). Allows full documents.
_ARTIFACT_BODY_CAP = 32_000

# ---------------------------------------------------------------------------
# Helpers: section extraction
# ---------------------------------------------------------------------------


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """Extract section between start_marker and end_marker (or end of text)."""
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _parse_bullet_list(section: str) -> List[str]:
    """Parse a section into list of strings (lines starting with - or * or numbered)."""
    if not section or not section.strip():
        return []
    items: List[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip leading "- ", "* ", "• ", or "N. " (e.g. "1. ")
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        if re.match(r"^\d+[.)]\s*", line):
            line = re.sub(r"^\d+[.)]\s*", "", line).strip()
        if line:
            items.append(line)
    return items


# ---------------------------------------------------------------------------
# Review output: passed, issues, recommendations, summary
# ---------------------------------------------------------------------------

MARKER_PASSED = "## PASSED ##"
MARKER_END_PASSED = "## END PASSED ##"
MARKER_ISSUES = "## ISSUES ##"
MARKER_END_ISSUES = "## END ISSUES ##"
MARKER_RECOMMENDATIONS = "## RECOMMENDATIONS ##"
MARKER_END_RECOMMENDATIONS = "## END RECOMMENDATIONS ##"
MARKER_SUMMARY = "## SUMMARY ##"
MARKER_END_SUMMARY = "## END SUMMARY ##"

_RE_NEXT_SECTION = re.compile(r"\n## [A-Z_]+ ##", re.DOTALL)
_RE_FILE_HEADER = re.compile(r"### (.+?) ###\s*", re.DOTALL)


def parse_review_output(text: str) -> Dict[str, Any]:
    """
    Parse review-phase template into passed, issues, recommendations, summary.

    Format:
      ## PASSED ##
      true or false
      ## END PASSED ##

      ## ISSUES ##
      - Issue 1
      - Issue 2
      ## END ISSUES ##

      ## RECOMMENDATIONS ##
      - Recommendation 1
      ## END RECOMMENDATIONS ##

      ## SUMMARY ##
      Summary text.
      ## END SUMMARY ##

    Returns dict with keys: "passed" (bool), "issues" (list of str), "recommendations" (list of str), "summary" (str).
    """
    passed = True
    issues: List[str] = []
    recommendations: List[str] = []
    summary = ""

    passed_section = _section(text, MARKER_PASSED, MARKER_END_PASSED)
    if passed_section:
        first = passed_section.strip().split("\n")[0].strip().lower()
        passed = first in ("true", "yes", "1", "pass")

    issues_section = _section(text, MARKER_ISSUES, MARKER_END_ISSUES)
    if issues_section:
        issues = _parse_bullet_list(issues_section)
    elif MARKER_ISSUES in text:
        idx = text.find(MARKER_ISSUES) + len(MARKER_ISSUES)
        rest = text[idx:].strip()
        if MARKER_RECOMMENDATIONS in rest:
            rest = rest.split(MARKER_RECOMMENDATIONS)[0].strip()
        if MARKER_SUMMARY in rest:
            rest = rest.split(MARKER_SUMMARY)[0].strip()
        issues = _parse_bullet_list(rest)

    rec_section = _section(text, MARKER_RECOMMENDATIONS, MARKER_END_RECOMMENDATIONS)
    if rec_section:
        recommendations = _parse_bullet_list(rec_section)
    elif MARKER_RECOMMENDATIONS in text:
        idx = text.find(MARKER_RECOMMENDATIONS) + len(MARKER_RECOMMENDATIONS)
        rest = text[idx:].strip()
        if MARKER_SUMMARY in rest:
            rest = rest.split(MARKER_SUMMARY)[0].strip()
        recommendations = _parse_bullet_list(rest)

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    if summary_section:
        summary = summary_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
    elif MARKER_SUMMARY in text:
        idx = text.find(MARKER_SUMMARY) + len(MARKER_SUMMARY)
        summary = text[idx:].strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]

    return {
        "passed": passed,
        "issues": issues,
        "recommendations": recommendations,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Planning output: goals_vision, key_features, milestones, etc.
# ---------------------------------------------------------------------------


def _section_to_str(section: str, max_len: int = 5000) -> str:
    """Take first paragraph or first line, cap length."""
    if not section:
        return ""
    s = section.strip()
    if "\n\n" in s:
        s = s.split("\n\n")[0].strip()
    return s.split("\n")[0].strip()[:max_len] if s else ""


def parse_planning_output(text: str) -> Dict[str, Any]:
    """
    Parse phase-level planning template into structured fields.

    Expects sections: GOALS_VISION, CONSTRAINTS_LIMITATIONS, KEY_FEATURES, MILESTONES,
    ARCHITECTURE, MAINTAINABILITY, SECURITY, FILE_SYSTEM, STYLING, DEPENDENCIES,
    MICROSERVICES, OTHERS, SUMMARY.

    Returns dict compatible with PlanningPhaseResult fields.
    """

    def get_section(name: str, max_len: int = _ARTIFACT_BODY_CAP) -> str:
        start = f"## {name} ##"
        end = f"## END {name} ##"
        return _section_to_str(_section(text, start, end), max_len)

    key_features_section = _section(text, "## KEY_FEATURES ##", "## END KEY_FEATURES ##")
    key_features = _parse_bullet_list(key_features_section) if key_features_section else []
    if not key_features and "## KEY_FEATURES ##" in text:
        idx = text.find("## KEY_FEATURES ##") + len("## KEY_FEATURES ##")
        rest = text[idx:].strip()
        if "## MILESTONES ##" in rest:
            rest = rest.split("## MILESTONES ##")[0].strip()
        key_features = _parse_bullet_list(rest)

    milestones_section = _section(text, "## MILESTONES ##", "## END MILESTONES ##")
    milestones = _parse_bullet_list(milestones_section) if milestones_section else []
    if not milestones and "## MILESTONES ##" in text:
        idx = text.find("## MILESTONES ##") + len("## MILESTONES ##")
        rest = text[idx:].strip()
        if "## ARCHITECTURE ##" in rest:
            rest = rest.split("## ARCHITECTURE ##")[0].strip()
        milestones = _parse_bullet_list(rest)

    dependencies_section = _section(text, "## DEPENDENCIES ##", "## END DEPENDENCIES ##")
    dependencies = _parse_bullet_list(dependencies_section) if dependencies_section else []
    if not dependencies and "## DEPENDENCIES ##" in text:
        idx = text.find("## DEPENDENCIES ##") + len("## DEPENDENCIES ##")
        rest = text[idx:].strip()
        if "## MICROSERVICES ##" in rest:
            rest = rest.split("## MICROSERVICES ##")[0].strip()
        dependencies = _parse_bullet_list(rest)

    return {
        "goals_vision": get_section("GOALS_VISION"),
        "constraints_limitations": get_section("CONSTRAINTS_LIMITATIONS"),
        "key_features": key_features,
        "milestones": milestones,
        "architecture": get_section("ARCHITECTURE"),
        "maintainability": get_section("MAINTAINABILITY"),
        "security": get_section("SECURITY"),
        "file_system": get_section("FILE_SYSTEM"),
        "styling": get_section("STYLING"),
        "dependencies": dependencies,
        "microservices": get_section("MICROSERVICES"),
        "others": get_section("OTHERS"),
        "summary": get_section("SUMMARY", _ARTIFACT_BODY_CAP) or "Planning complete.",
    }


# ---------------------------------------------------------------------------
# Fix output: root_cause, fix_description, resolved, file_updates
# ---------------------------------------------------------------------------

MARKER_ROOT_CAUSE = "## ROOT_CAUSE ##"
MARKER_END_ROOT_CAUSE = "## END ROOT_CAUSE ##"
MARKER_FIX_DESCRIPTION = "## FIX_DESCRIPTION ##"
MARKER_END_FIX_DESCRIPTION = "## END FIX_DESCRIPTION ##"
MARKER_RESOLVED = "## RESOLVED ##"
MARKER_END_RESOLVED = "## END RESOLVED ##"
MARKER_FILE_UPDATES = "## FILE_UPDATES ##"
MARKER_END_FILE_UPDATES = "## END FILE_UPDATES ##"
# LLM sometimes continues with this variant in later continuation chunks
MARKER_FILE_UPDATES_CONTINUED = "## FILE_UPDATES (CONTINUED) ##"
MARKER_END_FILE_UPDATES_CONTINUED = "## END FILE_UPDATES (CONTINUED) ##"
MARKER_END_FILE = "### END FILE ###"


def _collect_all_file_updates_sections(text: str) -> List[str]:
    """Collect every FILE_UPDATES block (including CONTINUED) from merged continuation text.

    When the LLM response is continued across multiple chunks, later chunks may add
    another ## FILE_UPDATES ## or ## FILE_UPDATES (CONTINUED) ## block. We need all
    of them so the written document includes the full content, not just the first block.
    """
    sections: List[str] = []
    markers_start = (MARKER_FILE_UPDATES, MARKER_FILE_UPDATES_CONTINUED)
    markers_end = (MARKER_END_FILE_UPDATES, MARKER_END_FILE_UPDATES_CONTINUED)
    pos = 0
    while pos < len(text):
        start_idx = -1
        start_marker = ""
        for m in markers_start:
            i = text.find(m, pos)
            if i != -1 and (start_idx == -1 or i < start_idx):
                start_idx = i
                start_marker = m
        if start_idx == -1:
            break
        content_start = start_idx + len(start_marker)
        end_idx = -1
        for m in markers_end:
            i = text.find(m, content_start)
            if i != -1 and (end_idx == -1 or i < end_idx):
                end_idx = i
        if end_idx == -1:
            end_idx = len(text)
        section = text[content_start:end_idx].strip()
        if section:
            sections.append(section)
        # Advance past the end marker we actually found (so we don't skip into next block)
        end_marker_len = 0
        for m in markers_end:
            if text[end_idx : end_idx + len(m)] == m:
                end_marker_len = len(m)
                break
        if not end_marker_len:
            end_marker_len = len(MARKER_END_FILE_UPDATES)
        pos = end_idx + end_marker_len
    return sections


def _parse_one_file_updates_section(updates_section: str) -> Dict[str, str]:
    """Parse a single FILE_UPDATES block into path -> content."""
    file_updates: Dict[str, str] = {}
    for m in _RE_FILE_HEADER.finditer(updates_section):
        path = m.group(1).strip()
        content_start = m.end()
        end_file = updates_section.find(MARKER_END_FILE, content_start)
        content_end = end_file if end_file != -1 else len(updates_section)
        content = updates_section[content_start:content_end].rstrip()
        if path:
            # Keep longest content per path (continuation often adds more to same file)
            if path not in file_updates or len(content) > len(file_updates[path]):
                file_updates[path] = content
    return file_updates


def parse_fix_output(text: str) -> Dict[str, Any]:
    """
    Parse single-issue fix template: root_cause, fix_description, resolved, file_updates.

    File updates format:
      ## FILE_UPDATES ##
      ### plan/planning_team/filename.md ###
      content here...
      ### END FILE ###
      ## END FILE_UPDATES ##

    Returns dict with keys: "root_cause", "fix_description", "resolved" (bool), "updated_content" (str for single file),
    and "file_updates" (dict path -> content) if multiple files.
    """
    root_cause = _section(text, MARKER_ROOT_CAUSE, MARKER_END_ROOT_CAUSE)
    root_cause = root_cause.strip().split("\n")[0].strip()[:1000] if root_cause else ""

    fix_description = _section(text, MARKER_FIX_DESCRIPTION, MARKER_END_FIX_DESCRIPTION)
    fix_description = (
        fix_description.strip().split("\n")[0].strip()[:1000] if fix_description else ""
    )

    resolved = True
    resolved_section = _section(text, MARKER_RESOLVED, MARKER_END_RESOLVED)
    if resolved_section:
        first = resolved_section.strip().split("\n")[0].strip().lower()
        resolved = first in ("true", "yes", "1")

    file_updates: Dict[str, str] = {}
    all_sections = _collect_all_file_updates_sections(text)
    if not all_sections and (MARKER_FILE_UPDATES in text or MARKER_FILE_UPDATES_CONTINUED in text):
        # Fallback: single block (original behavior)
        updates_section = _section(text, MARKER_FILE_UPDATES, MARKER_END_FILE_UPDATES)
        if not updates_section:
            idx = text.find(MARKER_FILE_UPDATES) + len(MARKER_FILE_UPDATES)
            updates_section = text[idx:].strip()
            if MARKER_END_FILE_UPDATES in updates_section:
                updates_section = updates_section.split(MARKER_END_FILE_UPDATES)[0].strip()
        if updates_section:
            all_sections = [updates_section]
    for section in all_sections:
        one = _parse_one_file_updates_section(section)
        for path, content in one.items():
            if path and (path not in file_updates or len(content) > len(file_updates[path])):
                file_updates[path] = content

    # Single file convenience: first path's content (primary artifact; all paths have longest from merge)
    updated_content = ""
    if file_updates:
        updated_content = next(iter(file_updates.values()), "")

    return {
        "root_cause": root_cause,
        "fix_description": fix_description,
        "resolved": resolved,
        "file_updates": file_updates,
        "updated_content": updated_content,
    }


def looks_like_truncated_file_content(content: str) -> bool:
    """Heuristically detect likely truncation of file content from fix_single_issue FILE_UPDATES.

    Returns True if the content appears to be cut off (do not write to disk).
    - Very short last non-empty line (e.g. < 20 chars) that is not a normal ending.
    - Content ends mid-word (last token not followed by space or newline).
    - Last line does not look like a complete sentence or section end (no sentence-ending punctuation).
    """
    if not content or not content.strip():
        return False
    stripped = content.rstrip()
    if not stripped:
        return False
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return False
    last_line = lines[-1].strip()
    # Very short last line that is not a normal ending (---, ##, - [ ], etc.)
    if len(last_line) < 20:
        normal_endings = ("---", "```", "***", "--- ", "- [ ]", "- [x]", "  ", "")
        if not any(last_line.startswith(p) or last_line == p for p in normal_endings):
            if not last_line.startswith("#") and not last_line.startswith("##"):
                return True
    # Ends mid-word: last character is alphanumeric (no space/newline/punctuation after last word)
    if last_line and last_line[-1].isalnum():
        # Could be end of a word at end of sentence; check if line ends with sentence punctuation
        if (
            len(last_line) > 1
            and last_line[-2].isalnum()
            and "." not in last_line
            and "!" not in last_line
            and "?" not in last_line
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Spec review / component analysis: components, integration_points, gaps, etc.
# ---------------------------------------------------------------------------


def parse_spec_review_output(text: str) -> Dict[str, Any]:
    """
    Parse spec review (e.g. system design) output: components, integration_points, gaps, scalability_notes, summary.

    Uses ## COMPONENTS ##, ## INTEGRATION_POINTS ##, ## GAPS ##, ## SCALABILITY_NOTES ##, ## SUMMARY ##.
    """
    components_section = _section(text, "## COMPONENTS ##", "## END COMPONENTS ##")
    components = _parse_bullet_list(components_section) if components_section else []
    if not components and "## COMPONENTS ##" in text:
        idx = text.find("## COMPONENTS ##") + len("## COMPONENTS ##")
        rest = text[idx:].strip()
        for end_marker in ("## INTEGRATION_POINTS ##", "## GAPS ##", "## SUMMARY ##"):
            if end_marker in rest:
                rest = rest.split(end_marker)[0].strip()
        components = _parse_bullet_list(rest)

    integration_section = _section(text, "## INTEGRATION_POINTS ##", "## END INTEGRATION_POINTS ##")
    integration_points = _parse_bullet_list(integration_section) if integration_section else []
    if not integration_points and "## INTEGRATION_POINTS ##" in text:
        idx = text.find("## INTEGRATION_POINTS ##") + len("## INTEGRATION_POINTS ##")
        rest = text[idx:].strip()
        if "## GAPS ##" in rest:
            rest = rest.split("## GAPS ##")[0].strip()
        integration_points = _parse_bullet_list(rest)

    gaps_section = _section(text, "## GAPS ##", "## END GAPS ##")
    gaps = _parse_bullet_list(gaps_section) if gaps_section else []
    if not gaps and "## GAPS ##" in text:
        idx = text.find("## GAPS ##") + len("## GAPS ##")
        rest = text[idx:].strip()
        if "## SCALABILITY_NOTES ##" in rest:
            rest = rest.split("## SCALABILITY_NOTES ##")[0].strip()
        if "## SUMMARY ##" in rest:
            rest = rest.split("## SUMMARY ##")[0].strip()
        gaps = _parse_bullet_list(rest)

    scalability_section = _section(text, "## SCALABILITY_NOTES ##", "## END SCALABILITY_NOTES ##")
    scalability_notes = (
        scalability_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if scalability_section
        else ""
    )

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    summary = (
        summary_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if summary_section
        else ""
    )

    return {
        "components": components,
        "integration_points": integration_points,
        "gaps": gaps,
        "scalability_notes": scalability_notes,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Planning tool output: recommendations, component_design, data_flow, etc.
# ---------------------------------------------------------------------------


def parse_planning_tool_output(text: str) -> Dict[str, Any]:
    """
    Parse tool agent planning output (e.g. system design): component_design, data_flow,
    integration_strategy, recommendations, summary.

    For component_design we accept bullet list of "name: responsibility" or "name - responsibility".
    """
    recommendations_section = _section(text, MARKER_RECOMMENDATIONS, MARKER_END_RECOMMENDATIONS)
    recommendations = _parse_bullet_list(recommendations_section) if recommendations_section else []
    if not recommendations and MARKER_RECOMMENDATIONS in text:
        idx = text.find(MARKER_RECOMMENDATIONS) + len(MARKER_RECOMMENDATIONS)
        rest = text[idx:].strip()
        if MARKER_SUMMARY in rest:
            rest = rest.split(MARKER_SUMMARY)[0].strip()
        recommendations = _parse_bullet_list(rest)

    data_flow_section = _section(text, "## DATA_FLOW ##", "## END DATA_FLOW ##")
    data_flow = data_flow_section.strip()[:_ARTIFACT_BODY_CAP] if data_flow_section else ""

    integration_section = _section(
        text, "## INTEGRATION_STRATEGY ##", "## END INTEGRATION_STRATEGY ##"
    )
    integration_strategy = (
        integration_section.strip()[:_ARTIFACT_BODY_CAP] if integration_section else ""
    )

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    summary = (
        summary_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if summary_section
        else ""
    )

    component_design: List[Dict[str, Any]] = []
    comp_section = _section(text, "## COMPONENT_DESIGN ##", "## END COMPONENT_DESIGN ##")
    if comp_section:
        for line in comp_section.splitlines():
            line = line.strip()
            if not line or line.startswith("-") or line.startswith("*"):
                continue
            name, _, rest = line.partition(":")
            if not name.strip():
                name, _, rest = line.partition(" - ")
            name = name.strip()
            if name:
                component_design.append(
                    {
                        "name": name,
                        "responsibility": rest.strip()[:_ARTIFACT_BODY_CAP] if rest else "",
                        "dependencies": [],
                    }
                )

    return {
        "component_design": component_design,
        "data_flow": data_flow,
        "integration_strategy": integration_strategy,
        "recommendations": recommendations,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Architecture planning: architecture_style, layers, cross_cutting, deployment_model, recommendations, summary
# ---------------------------------------------------------------------------


def parse_architecture_planning_output(text: str) -> Dict[str, Any]:
    """
    Parse architecture tool agent planning output.

    Returns dict with: architecture_style, layers (list of {name, technologies, responsibilities}),
    cross_cutting (list of str), deployment_model, recommendations, summary.
    """
    architecture_style = _section(text, "## ARCHITECTURE_STYLE ##", "## END ARCHITECTURE_STYLE ##")
    architecture_style = (
        architecture_style.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if architecture_style
        else ""
    )

    deployment_section = _section(text, "## DEPLOYMENT_MODEL ##", "## END DEPLOYMENT_MODEL ##")
    deployment_model = deployment_section.strip()[:_ARTIFACT_BODY_CAP] if deployment_section else ""

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    summary = (
        summary_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if summary_section
        else ""
    )

    rec_section = _section(text, MARKER_RECOMMENDATIONS, MARKER_END_RECOMMENDATIONS)
    recommendations = _parse_bullet_list(rec_section) if rec_section else []
    if not recommendations and MARKER_RECOMMENDATIONS in text:
        idx = text.find(MARKER_RECOMMENDATIONS) + len(MARKER_RECOMMENDATIONS)
        rest = text[idx:].strip()
        if MARKER_SUMMARY in rest:
            rest = rest.split(MARKER_SUMMARY)[0].strip()
        recommendations = _parse_bullet_list(rest)

    cross_section = _section(text, "## CROSS_CUTTING ##", "## END CROSS_CUTTING ##")
    cross_cutting = _parse_bullet_list(cross_section) if cross_section else []

    layers: List[Dict[str, Any]] = []
    layers_section = _section(text, "## LAYERS ##", "## END LAYERS ##")
    if layers_section:
        for line in layers_section.splitlines():
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            # Format: "LayerName: tech1, tech2 - responsibilities"
            if ":" in line:
                name_part, _, rest = line.partition(":")
                name = name_part.strip()
                if " - " in rest:
                    tech_part, _, resp = rest.partition(" - ")
                    technologies = [t.strip() for t in tech_part.split(",") if t.strip()]
                    responsibilities = resp.strip()
                else:
                    technologies = [rest.strip()] if rest.strip() else []
                    responsibilities = ""
                if name:
                    layers.append(
                        {
                            "name": name,
                            "technologies": technologies,
                            "responsibilities": responsibilities,
                        }
                    )

    return {
        "architecture_style": architecture_style,
        "layers": layers,
        "cross_cutting": cross_cutting,
        "deployment_model": deployment_model,
        "recommendations": recommendations,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# DevOps planning: recommendations, summary, needs_clarification, clarification_questions
# ---------------------------------------------------------------------------


def parse_devops_planning_output(text: str) -> Dict[str, Any]:
    """Parse DevOps planning output."""
    recommendations = _parse_bullet_list(
        _section(text, MARKER_RECOMMENDATIONS, MARKER_END_RECOMMENDATIONS)
    )
    summary = _section_to_str(
        _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY), _ARTIFACT_BODY_CAP
    )
    needs_section = _section(text, "## NEEDS_CLARIFICATION ##", "## END NEEDS_CLARIFICATION ##")
    needs_clarification = needs_section.strip().lower().startswith(
        "true"
    ) or needs_section.strip().lower().startswith("yes")
    q_section = _section(text, "## CLARIFICATION_QUESTIONS ##", "## END CLARIFICATION_QUESTIONS ##")
    clarification_questions = _parse_bullet_list(q_section) if q_section else []
    return {
        "recommendations": recommendations,
        "summary": summary or "DevOps planning complete.",
        "needs_clarification": needs_clarification,
        "clarification_questions": clarification_questions,
    }


# ---------------------------------------------------------------------------
# Task classification: classifications (list of {task_id, team, reason}), summary
# ---------------------------------------------------------------------------


def parse_task_classification_output(text: str) -> Dict[str, Any]:
    """Parse task classification output. Lines in CLASSIFICATIONS section: task_id | team | reason."""
    classifications: List[Dict[str, Any]] = []
    class_section = _section(text, "## CLASSIFICATIONS ##", "## END CLASSIFICATIONS ##")
    if class_section:
        for line in class_section.splitlines():
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                classifications.append(
                    {
                        "task_id": parts[0],
                        "team": parts[1],
                        "reason": parts[2] if len(parts) > 2 else "",
                    }
                )
    summary = _section_to_str(
        _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY), _ARTIFACT_BODY_CAP
    )
    return {
        "classifications": classifications,
        "summary": summary or "Task classification complete.",
    }


# ---------------------------------------------------------------------------
# Problem-solving phase: fixes_applied, resolved, summary
# ---------------------------------------------------------------------------

MARKER_FIXES_APPLIED = "## FIXES_APPLIED ##"
MARKER_END_FIXES_APPLIED = "## END FIXES_APPLIED ##"


def parse_problem_solving_output(text: str) -> Dict[str, Any]:
    """
    Parse problem-solving phase template: fixes_applied (list of str), resolved, summary.
    """
    fixes_section = _section(text, MARKER_FIXES_APPLIED, MARKER_END_FIXES_APPLIED)
    fixes_applied = _parse_bullet_list(fixes_section) if fixes_section else []
    if not fixes_applied and MARKER_FIXES_APPLIED in text:
        idx = text.find(MARKER_FIXES_APPLIED) + len(MARKER_FIXES_APPLIED)
        rest = text[idx:].strip()
        if MARKER_RESOLVED in rest:
            rest = rest.split(MARKER_RESOLVED)[0].strip()
        fixes_applied = _parse_bullet_list(rest)

    resolved = True
    resolved_section = _section(text, MARKER_RESOLVED, MARKER_END_RESOLVED)
    if resolved_section:
        first = resolved_section.strip().split("\n")[0].strip().lower()
        resolved = first in ("true", "yes", "1")

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    summary = (
        summary_section.strip().split("\n")[0].strip()[:_ARTIFACT_BODY_CAP]
        if summary_section
        else ""
    )

    return {
        "fixes_applied": fixes_applied,
        "resolved": resolved,
        "summary": summary,
    }
