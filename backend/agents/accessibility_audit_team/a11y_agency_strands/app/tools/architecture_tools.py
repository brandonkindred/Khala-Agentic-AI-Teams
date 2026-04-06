"""Architecture audit tools — thin wrappers around :class:`TemplateAuditEngine`.

Public API is preserved for backward compatibility and re-exported from
the tools ``__init__.py``.  All real logic lives in
:mod:`template_audit_engine`.
"""

from __future__ import annotations

from typing import Any

from ..models.architecture import ArchitectureAuditResult, ArchitectureSectionResult
from .template_audit_engine import TemplateAuditEngine

_TEMPLATE_NAME = "site_architecture_audit_template.yaml"


def _engine() -> TemplateAuditEngine:
    return TemplateAuditEngine(_TEMPLATE_NAME)


def load_architecture_audit_template() -> dict:
    """Load and cache the site architecture audit template YAML asset."""
    return _engine().template


def pct_to_grade(pct: float, template: dict | None = None) -> str:  # noqa: ARG001
    """Map a percentage to a grade label (template param kept for compat)."""
    return _engine().grading.grade(pct)


def score_architecture_section(
    section_id: str,
    section_name: str,
    results: list[dict[str, Any]],
    template: dict | None = None,  # noqa: ARG001
) -> ArchitectureSectionResult:
    """Score a single section (delegates to engine)."""
    return _engine().score_section(section_id, section_name, results)


def build_architecture_audit_report(
    target: str,
    section_results: list[ArchitectureSectionResult],
    recommendations: list[str] | None = None,
    template: dict | None = None,  # noqa: ARG001
) -> ArchitectureAuditResult:
    """Assemble a full report from scored sections (delegates to engine)."""
    return _engine().build_report(target, section_results, recommendations)
