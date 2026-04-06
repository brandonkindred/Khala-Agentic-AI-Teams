"""Tools for loading and scoring the Site Architecture & Navigation Accessibility Audit template."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from ..models.architecture import (
    ArchitectureAuditResult,
    ArchitectureChecklistItem,
    ArchitectureSectionResult,
    WCAGCriterionStatus,
)

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_TEMPLATE_CACHE: dict | None = None
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Template loading (thread-safe)
# ---------------------------------------------------------------------------


def load_architecture_audit_template() -> dict:
    """Load and cache the site architecture audit template YAML asset.

    Thread-safe: uses a lock to prevent concurrent reads from racing on
    the global cache.

    Returns:
        Parsed YAML dict with ``sections``, ``scoring``, ``methodology``, etc.
    """
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE
    with _CACHE_LOCK:
        # Double-check after acquiring the lock.
        if _TEMPLATE_CACHE is None:
            path = _ASSETS_DIR / "site_architecture_audit_template.yaml"
            with open(path) as fh:
                _TEMPLATE_CACHE = yaml.safe_load(fh)
    return _TEMPLATE_CACHE  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Grading — reads thresholds from the YAML template
# ---------------------------------------------------------------------------


def _grading_scale(template: dict | None = None) -> list[tuple[int, str]]:
    """Return ``[(min_pct, grade_label), ...]`` sorted descending by threshold.

    Reads from the template's ``scoring.grading_scale`` so changes to the
    YAML are reflected at runtime without a code change.
    """
    if template is None:
        template = load_architecture_audit_template()
    raw = template.get("scoring", {}).get("grading_scale", [])
    pairs = [(entry["min_pct"], entry["grade"]) for entry in raw]
    # Ensure descending order so the first match is the highest qualifying grade.
    pairs.sort(key=lambda t: t[0], reverse=True)
    return pairs


def pct_to_grade(pct: float, template: dict | None = None) -> str:
    """Map a percentage to a grade label using the template's grading scale."""
    for threshold, label in _grading_scale(template):
        if pct >= threshold:
            return label
    return "Poor"


# ---------------------------------------------------------------------------
# Section scoring
# ---------------------------------------------------------------------------


def score_architecture_section(
    section_id: str,
    section_name: str,
    results: list[dict[str, Any]],
    template: dict | None = None,
) -> ArchitectureSectionResult:
    """Score a single architecture audit section from checklist results.

    Items with ``passed=None`` are treated as "not tested" and excluded
    from the score calculation.  Only items with an explicit ``True`` or
    ``False`` contribute.

    Args:
        section_id: Identifier of the section being scored.
        section_name: Display name.
        results: List of dicts, each with at least ``id``, ``label``,
            and optionally ``passed`` (bool | None), ``notes``,
            ``wcag_ref``, ``wcag_level``, ``test_method``.
        template: Optional pre-loaded template (avoids repeated YAML reads).

    Returns:
        :class:`ArchitectureSectionResult` with counts, percentage, and grade.
    """
    items = [ArchitectureChecklistItem(**r) for r in results]
    total = len(items)
    tested = [it for it in items if it.passed is not None]
    tested_count = len(tested)
    passed = sum(1 for it in tested if it.passed)
    pct = (passed / tested_count * 100) if tested_count else 0.0
    issues = [it.label for it in items if it.passed is False]

    return ArchitectureSectionResult(
        section_id=section_id,
        name=section_name,
        items=items,
        tested_count=tested_count,
        passed_count=passed,
        total_count=total,
        score_pct=round(pct, 1),
        grade=pct_to_grade(pct, template),
        issues=issues,
    )


# ---------------------------------------------------------------------------
# WCAG compliance aggregation
# ---------------------------------------------------------------------------

# Map of SC number → human-readable name (navigation-relevant subset).
_SC_NAMES: dict[str, str] = {
    "1.3.1": "Info and Relationships",
    "1.3.2": "Meaningful Sequence",
    "1.3.3": "Sensory Characteristics",
    "1.4.4": "Resize Text",
    "1.4.10": "Reflow",
    "2.1.1": "Keyboard",
    "2.1.2": "No Keyboard Trap",
    "2.3.3": "Animation from Interactions",
    "2.4.1": "Bypass Blocks",
    "2.4.2": "Page Titled",
    "2.4.3": "Focus Order",
    "2.4.4": "Link Purpose (In Context)",
    "2.4.5": "Multiple Ways",
    "2.4.7": "Focus Visible",
    "2.4.8": "Location",
    "2.5.1": "Pointer Gestures",
    "2.5.8": "Target Size (Minimum)",
    "3.2.1": "On Focus",
    "3.2.2": "On Input",
    "3.2.3": "Consistent Navigation",
    "3.2.4": "Consistent Identification",
    "3.3.1": "Error Identification",
    "3.3.2": "Labels or Instructions",
    "4.1.2": "Name, Role, Value",
    "4.1.3": "Status Messages",
}


def _collect_wcag_statuses(sections: list[ArchitectureSectionResult]) -> list[WCAGCriterionStatus]:
    """Derive per-criterion pass/fail status from scored section items.

    Also populates the human-readable ``name`` and ``wcag_level`` fields
    when available from the checklist item or the built-in SC-name map.
    """
    sc_map: dict[str, dict[str, Any]] = {}
    for section in sections:
        for item in section.items:
            if not item.wcag_ref:
                continue
            sc = item.wcag_ref
            if sc not in sc_map:
                sc_map[sc] = {
                    "items": [],
                    "passed": [],
                    "failed": [],
                    "not_tested": [],
                    "wcag_level": "",
                }
            sc_map[sc]["items"].append(item.id)
            if item.passed is True:
                sc_map[sc]["passed"].append(item.id)
            elif item.passed is False:
                sc_map[sc]["failed"].append(item.id)
            else:
                sc_map[sc]["not_tested"].append(item.id)
            # Capture wcag_level from item if present (e.g. from the WCAG
            # Compliance Summary section which carries wcag_level per item).
            if item.wcag_level and not sc_map[sc]["wcag_level"]:
                sc_map[sc]["wcag_level"] = item.wcag_level

    statuses: list[WCAGCriterionStatus] = []
    for sc, data in sorted(sc_map.items()):
        if data["failed"]:
            status = "fail" if not data["passed"] else "partial"
        elif data["passed"]:
            status = "pass"
        else:
            status = "not_tested"
        statuses.append(
            WCAGCriterionStatus(
                sc=sc,
                name=_SC_NAMES.get(sc, ""),
                wcag_level=data["wcag_level"],
                status=status,
                related_items=data["items"],
            )
        )
    return statuses


# ---------------------------------------------------------------------------
# Full report assembly
# ---------------------------------------------------------------------------


def build_architecture_audit_report(
    target: str,
    section_results: list[ArchitectureSectionResult],
    recommendations: list[str] | None = None,
    template: dict | None = None,
) -> ArchitectureAuditResult:
    """Assemble a full architecture audit report from scored sections.

    The overall score is the **mean of section percentages** (equal section
    weighting) rather than a raw item-count-weighted average, so that
    sections with fewer items are not under-represented.

    Args:
        target: Site URL or identifier.
        section_results: Scored section results from
            :func:`score_architecture_section`.
        recommendations: Optional prioritized recommendation strings.
        template: Optional pre-loaded template (avoids repeated YAML reads).

    Returns:
        :class:`ArchitectureAuditResult` with overall score, grade, WCAG
        compliance mapping, and recommendations.
    """
    scored_sections = [s for s in section_results if s.tested_count > 0]
    if scored_sections:
        overall_pct = sum(s.score_pct for s in scored_sections) / len(scored_sections)
    else:
        overall_pct = 0.0

    wcag_compliance = _collect_wcag_statuses(section_results)

    return ArchitectureAuditResult(
        target=target,
        sections=section_results,
        overall_score_pct=round(overall_pct, 1),
        overall_grade=pct_to_grade(overall_pct, template),
        wcag_compliance=wcag_compliance,
        recommendations=recommendations or [],
    )
