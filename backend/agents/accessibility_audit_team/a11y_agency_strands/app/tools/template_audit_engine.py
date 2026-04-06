"""Generic template-driven audit engine.

Owns the complete pipeline for any YAML-based accessibility audit:
load template → walk sections → apply overrides → score → build report.

The architecture audit is the first consumer; future template-based audits
(forms, media, ARIA patterns, etc.) reuse the same engine with a different
YAML template filename.
"""

from __future__ import annotations

import re
from typing import Any

from ..models.architecture import (
    ArchitectureAuditResult,
    ArchitectureChecklistItem,
    ArchitectureSectionResult,
    WCAGCriterionStatus,
)
from ..models.grading import GradingScale
from .asset_registry import AssetRegistry
from .storage_tools import persist_artifact

# ---------------------------------------------------------------------------
# SC name extraction (refactoring #6 — derive from YAML, not Python)
# ---------------------------------------------------------------------------

# Regex to pull "X.Y.Z Name" from labels like
# "2.4.7 Focus Visible - Visible focus indicator on all navigation items"
_SC_LABEL_RE = re.compile(r"^(\d+\.\d+\.\d+)\s+(.+?)(?:\s*-|$)")


def _extract_sc_names_from_template(template: dict) -> dict[str, str]:
    """Build an SC-number → human-readable-name map from the WCAG
    Compliance Summary section labels.

    Falls back to an empty string for criteria not present in the template.
    """
    sc_names: dict[str, str] = {}
    for section in template.get("sections", []):
        if section.get("id") != "wcag_compliance_summary":
            continue
        for sub in section.get("subsections", []):
            for item in sub.get("checklist_items", []):
                label = item.get("label", "")
                match = _SC_LABEL_RE.match(label)
                if match:
                    sc_names[match.group(1)] = match.group(2).strip()
    return sc_names


# ---------------------------------------------------------------------------
# Shared helpers (refactoring #5 — single public flattener)
# ---------------------------------------------------------------------------


def flatten_checklist_items(section_def: dict) -> list[dict]:
    """Extract all checklist items from a template section's subsections.

    This is the **single source of truth** for walking the
    ``subsections → checklist_items`` hierarchy in any YAML audit template.
    """
    items: list[dict] = []
    for sub in section_def.get("subsections", []):
        for item in sub.get("checklist_items", []):
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TemplateAuditEngine:
    """Generic pipeline for YAML-template-driven accessibility audits.

    Usage::

        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        report = engine.evaluate("https://example.com", overrides, recs)
        path   = engine.persist(report, "/artifacts/eng-123")
    """

    def __init__(self, template_name: str) -> None:
        self.template: dict = AssetRegistry.load(template_name)
        self.grading: GradingScale = GradingScale.from_template(self.template)
        self._sc_names: dict[str, str] = _extract_sc_names_from_template(self.template)

    # -- scoring -----------------------------------------------------------

    def score_section(
        self,
        section_id: str,
        section_name: str,
        results: list[dict[str, Any]],
    ) -> ArchitectureSectionResult:
        """Score a single section from a list of checklist-item result dicts."""
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
            grade=self.grading.grade(pct),
            issues=issues,
        )

    # -- WCAG compliance ---------------------------------------------------

    def _collect_wcag_statuses(
        self, sections: list[ArchitectureSectionResult]
    ) -> list[WCAGCriterionStatus]:
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
                    name=self._sc_names.get(sc, ""),
                    wcag_level=data["wcag_level"],
                    status=status,
                    related_items=data["items"],
                )
            )
        return statuses

    # -- full evaluate pipeline --------------------------------------------

    def evaluate(
        self,
        target: str,
        overrides: dict[str, dict[str, Any]] | None = None,
        recommendations: list[str] | None = None,
    ) -> ArchitectureAuditResult:
        """Run the full template-driven audit pipeline.

        Args:
            target: URL or identifier being audited.
            overrides: Map of checklist-item ID →
                ``{"passed": bool | None, "notes": str}``.
            recommendations: Prioritized recommendation strings.

        Returns:
            Fully scored :class:`ArchitectureAuditResult`.
        """
        overrides = overrides or {}
        section_results: list[ArchitectureSectionResult] = []

        for section_def in self.template.get("sections", []):
            section_id = section_def["id"]
            section_name = section_def["name"]
            template_items = flatten_checklist_items(section_def)

            evaluated: list[dict] = []
            for item in template_items:
                item_id = item["id"]
                override = overrides.get(item_id, {})
                evaluated.append(
                    {
                        "id": item_id,
                        "label": item.get("label", ""),
                        "passed": override.get("passed"),
                        "notes": override.get("notes", ""),
                        "wcag_ref": item.get("wcag_ref"),
                        "wcag_level": item.get("wcag_level"),
                        "test_method": item.get("test_method", ""),
                    }
                )

            scored = self.score_section(section_id, section_name, evaluated)
            section_results.append(scored)

        return self.build_report(target, section_results, recommendations)

    def build_report(
        self,
        target: str,
        section_results: list[ArchitectureSectionResult],
        recommendations: list[str] | None = None,
    ) -> ArchitectureAuditResult:
        """Assemble a full report from scored sections (equal section weighting)."""
        scored = [s for s in section_results if s.tested_count > 0]
        overall_pct = sum(s.score_pct for s in scored) / len(scored) if scored else 0.0
        wcag_compliance = self._collect_wcag_statuses(section_results)

        return ArchitectureAuditResult(
            target=target,
            sections=section_results,
            overall_score_pct=round(overall_pct, 1),
            overall_grade=self.grading.grade(overall_pct),
            wcag_compliance=wcag_compliance,
            recommendations=recommendations or [],
        )

    # -- persistence -------------------------------------------------------

    def persist(self, result: ArchitectureAuditResult, artifact_root: str) -> str:
        """Persist the audit result as ``architecture.json`` under *artifact_root*."""
        return persist_artifact(f"{artifact_root}/architecture.json", result.model_dump())
