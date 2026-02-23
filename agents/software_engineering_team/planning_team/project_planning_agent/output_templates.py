"""
Template-based output parsing for the Project Planning agent.

Avoids JSON so the LLM can produce long markdown (e.g. features_and_functionality)
without truncation breaking parse. Section-delimited text allows partial output
to still yield useful results when the response is cut off.
"""

from __future__ import annotations

from typing import Any, Dict, List

BLOCK_SEP = "---"

# Section markers (must appear at start of line)
MARKER_FEATURES = "## FEATURES_AND_FUNCTIONALITY ##"
MARKER_END_FEATURES = "## END FEATURES_AND_FUNCTIONALITY ##"
MARKER_PRIMARY_GOAL = "## PRIMARY_GOAL ##"
MARKER_END_PRIMARY_GOAL = "## END PRIMARY_GOAL ##"
MARKER_SECONDARY_GOALS = "## SECONDARY_GOALS ##"
MARKER_END_SECONDARY_GOALS = "## END SECONDARY_GOALS ##"
MARKER_DELIVERY_STRATEGY = "## DELIVERY_STRATEGY ##"
MARKER_END_DELIVERY_STRATEGY = "## END DELIVERY_STRATEGY ##"
MARKER_SCOPE_CUT = "## SCOPE_CUT ##"
MARKER_END_SCOPE_CUT = "## END SCOPE_CUT ##"
MARKER_SUMMARY = "## SUMMARY ##"
MARKER_END_SUMMARY = "## END SUMMARY ##"
MARKER_MILESTONES = "## MILESTONES ##"
MARKER_END_MILESTONES = "## END MILESTONES ##"
MARKER_RISK_ITEMS = "## RISK_ITEMS ##"
MARKER_END_RISK_ITEMS = "## END RISK_ITEMS ##"
MARKER_EPIC_STORY_BREAKDOWN = "## EPIC_STORY_BREAKDOWN ##"
MARKER_END_EPIC_STORY_BREAKDOWN = "## END EPIC_STORY_BREAKDOWN ##"
MARKER_NON_FUNCTIONAL_REQUIREMENTS = "## NON_FUNCTIONAL_REQUIREMENTS ##"
MARKER_END_NFR = "## END NON_FUNCTIONAL_REQUIREMENTS ##"


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


def _parse_key_value_block(block: str) -> Dict[str, Any]:
    """Parse a block of key: value lines. Pipe-separated values become lists."""
    out: Dict[str, Any] = {}
    list_keys = {"secondary_goals", "dependencies", "non_functional_requirements"}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key in list_keys and value:
            out[key] = [v.strip() for v in value.split("|") if v.strip()]
        elif key == "target_order" and value:
            try:
                out[key] = int(value)
            except ValueError:
                out[key] = 0
        elif value:
            out[key] = value
    return out


def _blocks_in_section(section_text: str) -> List[str]:
    """Split section by --- into blocks."""
    if not section_text.strip():
        return []
    blocks: List[str] = []
    for part in section_text.split(BLOCK_SEP):
        part = part.strip()
        if part:
            blocks.append(part)
    return blocks


def parse_project_planning_template(text: str) -> Dict[str, Any]:
    """
    Parse template output into project overview fields.

    Tolerant to truncation: if an end marker is missing, content up to the next
    section or end of text is used. Returns a dict suitable for building
    ProjectOverview (features_and_functionality, primary_goal, secondary_goals,
    milestones, risk_items, delivery_strategy, epic_story_breakdown, scope_cut,
    non_functional_requirements, summary).
    """
    result: Dict[str, Any] = {
        "features_and_functionality": "",
        "primary_goal": "",
        "secondary_goals": [],
        "delivery_strategy": "",
        "scope_cut": "",
        "summary": "",
        "milestones": [],
        "risk_items": [],
        "epic_story_breakdown": [],
        "non_functional_requirements": [],
    }

    # Features doc (markdown; can be long)
    features = _section(text, MARKER_FEATURES, MARKER_END_FEATURES)
    if not features and MARKER_FEATURES in text:
        idx = text.find(MARKER_FEATURES) + len(MARKER_FEATURES)
        features = text[idx:].strip()
        # Stop at next section if present
        for marker in (
            MARKER_PRIMARY_GOAL, MARKER_MILESTONES, MARKER_RISK_ITEMS,
            MARKER_DELIVERY_STRATEGY, MARKER_EPIC_STORY_BREAKDOWN, MARKER_SCOPE_CUT,
            MARKER_NON_FUNCTIONAL_REQUIREMENTS, MARKER_SUMMARY,
        ):
            if marker in features:
                features = features.split(marker)[0].strip()
                break
    result["features_and_functionality"] = features

    # Primary goal (single line)
    primary = _section(text, MARKER_PRIMARY_GOAL, MARKER_END_PRIMARY_GOAL)
    if not primary and MARKER_PRIMARY_GOAL in text:
        idx = text.find(MARKER_PRIMARY_GOAL) + len(MARKER_PRIMARY_GOAL)
        primary = text[idx:].strip().split("\n")[0].strip()
        if MARKER_SECONDARY_GOALS in primary:
            primary = primary.split(MARKER_SECONDARY_GOALS)[0].strip()
    result["primary_goal"] = primary.split("\n")[0].strip() if primary else ""

    # Secondary goals (list: one per line or pipe-separated in one block)
    sec = _section(text, MARKER_SECONDARY_GOALS, MARKER_END_SECONDARY_GOALS)
    if sec:
        lines = [ln.strip() for ln in sec.splitlines() if ln.strip() and not ln.strip().startswith("-")]
        if not lines and sec.strip():
            lines = [s.strip() for s in sec.replace("|", "\n").split("\n") if s.strip()]
        result["secondary_goals"] = [ln.lstrip("- ").strip() for ln in lines if ln][:10]
    elif MARKER_SECONDARY_GOALS in text:
        idx = text.find(MARKER_SECONDARY_GOALS) + len(MARKER_SECONDARY_GOALS)
        sec = text[idx:].strip()
        if MARKER_DELIVERY_STRATEGY in sec:
            sec = sec.split(MARKER_DELIVERY_STRATEGY)[0].strip()
        result["secondary_goals"] = [ln.strip().lstrip("- ") for ln in sec.splitlines() if ln.strip()][:10]

    # Delivery strategy
    ds = _section(text, MARKER_DELIVERY_STRATEGY, MARKER_END_DELIVERY_STRATEGY)
    if not ds and MARKER_DELIVERY_STRATEGY in text:
        idx = text.find(MARKER_DELIVERY_STRATEGY) + len(MARKER_DELIVERY_STRATEGY)
        ds = text[idx:].strip().split("\n")[0].strip()
        if MARKER_SCOPE_CUT in ds:
            ds = ds.split(MARKER_SCOPE_CUT)[0].strip()
    result["delivery_strategy"] = ds.split("\n")[0].strip()[:2000] if ds else ""

    # Scope cut
    scope = _section(text, MARKER_SCOPE_CUT, MARKER_END_SCOPE_CUT)
    if not scope and MARKER_SCOPE_CUT in text:
        idx = text.find(MARKER_SCOPE_CUT) + len(MARKER_SCOPE_CUT)
        scope = text[idx:].strip().split("\n")[0].strip()
        if MARKER_NON_FUNCTIONAL_REQUIREMENTS in scope or MARKER_SUMMARY in scope:
            for m in (MARKER_NON_FUNCTIONAL_REQUIREMENTS, MARKER_SUMMARY):
                if m in scope:
                    scope = scope.split(m)[0].strip()
                    break
    result["scope_cut"] = scope.strip()[:2000] if scope else ""

    # Summary
    summary = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    if not summary and MARKER_SUMMARY in text:
        idx = text.find(MARKER_SUMMARY) + len(MARKER_SUMMARY)
        summary = text[idx:].strip().split("\n")[0].strip()[:1000]
    result["summary"] = summary.split("\n")[0].strip()[:1000] if summary else ""

    # Milestones (blocks with id, name, description, target_order, scope_summary, definition_of_done)
    ms_section = _section(text, MARKER_MILESTONES, MARKER_END_MILESTONES)
    if not ms_section and MARKER_MILESTONES in text:
        idx = text.find(MARKER_MILESTONES) + len(MARKER_MILESTONES)
        ms_section = text[idx:].strip()
        if MARKER_RISK_ITEMS in ms_section:
            ms_section = ms_section.split(MARKER_RISK_ITEMS)[0].strip()
    for block in _blocks_in_section(ms_section):
        obj = _parse_key_value_block(block)
        if obj.get("id"):
            obj.setdefault("name", "")
            obj.setdefault("description", "")
            obj.setdefault("target_order", len(result["milestones"]))
            obj.setdefault("scope_summary", "")
            obj.setdefault("definition_of_done", "")
            result["milestones"].append(obj)

    # Risk items (blocks with description, severity, mitigation)
    risk_section = _section(text, MARKER_RISK_ITEMS, MARKER_END_RISK_ITEMS)
    if not risk_section and MARKER_RISK_ITEMS in text:
        idx = text.find(MARKER_RISK_ITEMS) + len(MARKER_RISK_ITEMS)
        risk_section = text[idx:].strip()
        if MARKER_EPIC_STORY_BREAKDOWN in risk_section or MARKER_DELIVERY_STRATEGY in risk_section:
            for m in (MARKER_EPIC_STORY_BREAKDOWN, MARKER_DELIVERY_STRATEGY):
                if m in risk_section:
                    risk_section = risk_section.split(m)[0].strip()
                    break
    for block in _blocks_in_section(risk_section):
        obj = _parse_key_value_block(block)
        if obj.get("description"):
            obj.setdefault("severity", "medium")
            obj.setdefault("mitigation", "")
            result["risk_items"].append(obj)

    # Epic/story breakdown (blocks with id, name, description, scope, dependencies)
    epic_section = _section(text, MARKER_EPIC_STORY_BREAKDOWN, MARKER_END_EPIC_STORY_BREAKDOWN)
    if not epic_section and MARKER_EPIC_STORY_BREAKDOWN in text:
        idx = text.find(MARKER_EPIC_STORY_BREAKDOWN) + len(MARKER_EPIC_STORY_BREAKDOWN)
        epic_section = text[idx:].strip()
        if MARKER_SCOPE_CUT in epic_section:
            epic_section = epic_section.split(MARKER_SCOPE_CUT)[0].strip()
    for block in _blocks_in_section(epic_section):
        obj = _parse_key_value_block(block)
        if obj.get("id"):
            obj.setdefault("name", "")
            obj.setdefault("description", "")
            obj.setdefault("scope", "MVP")
            obj.setdefault("dependencies", [])
            if isinstance(obj.get("dependencies"), str):
                obj["dependencies"] = [v.strip() for v in obj["dependencies"].split("|") if v.strip()]
            result["epic_story_breakdown"].append(obj)

    # Non-functional requirements (list of lines)
    nfr_section = _section(text, MARKER_NON_FUNCTIONAL_REQUIREMENTS, MARKER_END_NFR)
    if not nfr_section and MARKER_NON_FUNCTIONAL_REQUIREMENTS in text:
        idx = text.find(MARKER_NON_FUNCTIONAL_REQUIREMENTS) + len(MARKER_NON_FUNCTIONAL_REQUIREMENTS)
        nfr_section = text[idx:].strip()
        if MARKER_SUMMARY in nfr_section:
            nfr_section = nfr_section.split(MARKER_SUMMARY)[0].strip()
    if nfr_section:
        result["non_functional_requirements"] = [
            ln.strip().lstrip("- ").strip() for ln in nfr_section.splitlines() if ln.strip()
        ][:20]

    return result
